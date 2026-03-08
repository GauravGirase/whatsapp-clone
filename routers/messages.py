from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update
from sqlalchemy.orm import selectinload
from db.database import get_db
from models.models import (
    Message, Conversation, ConversationMember, MessageReceipt,
    MessageReaction, MessageStatus, MessageType, User
)
from schemas.schemas import (
    MessageOut, MessagePage, SendMessage, EditMessage,
    ForwardMessage, AddReaction, ReadReceipt
)
from core.auth import get_current_user
from core.redis_manager import pubsub_manager, cache_manager, pending_queue, presence_manager
from core.storage import upload_media
from datetime import datetime, timezone
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/messages", tags=["Messages"])


# ─── Send ─────────────────────────────────────────────────────────────────────

@router.post("", response_model=MessageOut, status_code=201)
async def send_message(
    data: SendMessage,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    member = await _assert_member(db, data.conversation_id, current_user.id)

    msg = Message(
        conversation_id=data.conversation_id,
        sender_id=current_user.id,
        type=data.type,
        content=data.content,
        reply_to_id=data.reply_to_id,
        metadata=data.metadata,
        status=MessageStatus.SENT,
    )
    db.add(msg)

    # Update conversation last_message_at
    await db.execute(
        update(Conversation)
        .where(Conversation.id == data.conversation_id)
        .values(last_message_at=datetime.now(timezone.utc))
    )

    await db.flush()
    await db.refresh(msg)

    # Load sender for response
    result = await db.execute(select(User).where(User.id == current_user.id))
    msg.sender = result.scalar_one()

    # Fan out to all members
    members = await _get_member_ids(db, data.conversation_id)
    payload = _message_to_payload(msg, "new_message")
    await _fanout(members, payload, exclude=current_user.id)

    # Invalidate conversation list cache
    for uid in members:
        await cache_manager.delete(f"convlist:{uid}")

    logger.info("Message sent", msg_id=msg.id, conv=data.conversation_id)
    return msg


@router.post("/media", response_model=MessageOut, status_code=201)
async def send_media_message(
    conversation_id: str,
    file: UploadFile = File(...),
    reply_to_id: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_member(db, conversation_id, current_user.id)

    media_url, media_type, size, duration, thumbnail = await upload_media(file, current_user.id)

    msg = Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        type=media_type,
        media_url=media_url,
        media_thumbnail=thumbnail,
        media_size=size,
        media_duration=duration,
        reply_to_id=reply_to_id,
        status=MessageStatus.SENT,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)

    members = await _get_member_ids(db, conversation_id)
    payload = _message_to_payload(msg, "new_message")
    await _fanout(members, payload, exclude=current_user.id)
    return msg


# ─── Read ─────────────────────────────────────────────────────────────────────

@router.get("/{conversation_id}", response_model=MessagePage)
async def get_messages(
    conversation_id: str,
    limit: int = Query(50, ge=1, le=100),
    before: str = Query(None, description="Message ID cursor for pagination"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_member(db, conversation_id, current_user.id)

    query = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.deleted_at == None,
        )
        .options(
            selectinload(Message.sender),
            selectinload(Message.reactions),
        )
        .order_by(Message.created_at.desc())
        .limit(limit + 1)
    )

    if before:
        cursor_result = await db.execute(select(Message).where(Message.id == before))
        cursor_msg = cursor_result.scalar_one_or_none()
        if cursor_msg:
            query = query.where(Message.created_at < cursor_msg.created_at)

    result = await db.execute(query)
    messages = result.scalars().all()
    has_more = len(messages) > limit
    messages = list(reversed(messages[:limit]))

    next_cursor = messages[0].id if has_more and messages else None
    messages_out = [MessageOut.model_validate(m, strict=False) for m in messages]

    return MessagePage(
        messages=messages_out,
        has_more=has_more,
        next_cursor=next_cursor
    )
    # return MessagePage(messages=messages, has_more=has_more, next_cursor=next_cursor)


# ─── Edit ─────────────────────────────────────────────────────────────────────

@router.patch("/{message_id}", response_model=MessageOut)
async def edit_message(
    message_id: str,
    data: EditMessage,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    msg = await _get_own_message(db, message_id, current_user.id)
    if msg.type != MessageType.TEXT:
        raise HTTPException(status_code=400, detail="Only text messages can be edited")

    msg.content = data.content
    msg.is_edited = True
    msg.edited_at = datetime.now(timezone.utc)
    await db.flush()

    members = await _get_member_ids(db, msg.conversation_id)
    await _fanout(members, _message_to_payload(msg, "message_updated"))
    return msg


# ─── Delete ───────────────────────────────────────────────────────────────────

@router.delete("/{message_id}", status_code=204)
async def delete_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    msg = await _get_own_message(db, message_id, current_user.id)
    msg.deleted_at = datetime.now(timezone.utc)
    msg.content = None
    msg.type = MessageType.DELETED

    members = await _get_member_ids(db, msg.conversation_id)
    await _fanout(members, {
        "type": "message_deleted",
        "message_id": message_id,
        "conversation_id": msg.conversation_id,
    })


# ─── Forward ──────────────────────────────────────────────────────────────────

@router.post("/forward", response_model=list[MessageOut], status_code=201)
async def forward_message(
    data: ForwardMessage,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Message).where(Message.id == data.message_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Message not found")

    forwarded = []
    for conv_id in data.conversation_ids:
        await _assert_member(db, conv_id, current_user.id)
        msg = Message(
            conversation_id=conv_id,
            sender_id=current_user.id,
            type=source.type,
            content=source.content,
            media_url=source.media_url,
            media_thumbnail=source.media_thumbnail,
            media_size=source.media_size,
            is_forwarded=True,
            status=MessageStatus.SENT,
        )
        db.add(msg)
        await db.flush()
        members = await _get_member_ids(db, conv_id)
        await _fanout(members, _message_to_payload(msg, "new_message"), exclude=current_user.id)
        forwarded.append(msg)

    return forwarded


# ─── Reactions ────────────────────────────────────────────────────────────────

@router.post("/{message_id}/reactions", status_code=204)
async def add_reaction(
    message_id: str,
    data: AddReaction,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Message).where(Message.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    await _assert_member(db, msg.conversation_id, current_user.id)

    # Toggle: remove if same emoji already exists
    existing = await db.execute(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == current_user.id,
            MessageReaction.emoji == data.emoji,
        )
    )
    reaction = existing.scalar_one_or_none()
    if reaction:
        await db.delete(reaction)
    else:
        db.add(MessageReaction(message_id=message_id, user_id=current_user.id, emoji=data.emoji))

    members = await _get_member_ids(db, msg.conversation_id)
    await _fanout(members, {
        "type": "reaction_updated",
        "message_id": message_id,
        "user_id": current_user.id,
        "emoji": data.emoji,
        "action": "removed" if reaction else "added",
    })


# ─── Read Receipts ────────────────────────────────────────────────────────────

@router.post("/receipts/read", status_code=204)
async def mark_read(
    data: ReadReceipt,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    for msg_id in data.message_ids:
        result = await db.execute(
            select(MessageReceipt).where(
                MessageReceipt.message_id == msg_id,
                MessageReceipt.user_id == current_user.id,
            )
        )
        receipt = result.scalar_one_or_none()
        if receipt:
            receipt.status = MessageStatus.READ
        else:
            db.add(MessageReceipt(
                message_id=msg_id,
                user_id=current_user.id,
                status=MessageStatus.READ,
            ))

    # Notify senders
    msg_result = await db.execute(
        select(Message.id, Message.sender_id).where(Message.id.in_(data.message_ids))
    )
    for msg_id, sender_id in msg_result.all():
        await pubsub_manager.publish_to_user(sender_id, {
            "type": "read_receipt",
            "message_id": msg_id,
            "read_by": current_user.id,
            "status": "read",
        })


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _assert_member(db, conv_id, user_id):
    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conv_id,
            ConversationMember.user_id == user_id,
            ConversationMember.left_at == None,
        )
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=403, detail="Not a member of this conversation")
    return m


async def _get_own_message(db, message_id, user_id):
    result = await db.execute(select(Message).where(Message.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_id != user_id:
        raise HTTPException(status_code=403, detail="Cannot modify another user's message")
    return msg


async def _get_member_ids(db, conv_id) -> list[str]:
    result = await db.execute(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conv_id,
            ConversationMember.left_at == None,
        )
    )
    return [r[0] for r in result.all()]


async def _fanout(member_ids: list[str], payload: dict, exclude: str = None):
    """Send payload to all members. Routes via local WS or Redis pub/sub."""
    from core.websocket_manager import ws_manager
    for uid in member_ids:
        if uid == exclude:
            continue
        delivered = await ws_manager.send_to_user(uid, payload)
        if not delivered:
            # User not on this server — pub/sub for other instances OR queue for offline
            is_online = await presence_manager.is_online(uid)
            if is_online:
                await pubsub_manager.publish_to_user(uid, payload)
            else:
                await pending_queue.push(uid, payload)


def _message_to_payload(msg: Message, event_type: str) -> dict:
    return {
        "type": event_type,
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "sender_id": msg.sender_id,
        "message_type": msg.type,
        "content": msg.content,
        "media_url": msg.media_url,
        "reply_to_id": msg.reply_to_id,
        "is_edited": msg.is_edited,
        "status": msg.status,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }
