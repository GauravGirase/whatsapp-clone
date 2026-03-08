from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload
from db.database import get_db
from models.models import (
    Conversation, ConversationMember, User,
    ConversationType, MemberRole, Message
)
from schemas.schemas import (
    ConversationOut, CreateDirectConversation,
    CreateGroupConversation, UpdateGroup, AddMembers, MemberOut
)
from core.auth import get_current_user
from core.redis_manager import cache_manager, pubsub_manager
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/conversations", tags=["Conversations"])


async def _get_or_create_direct(db, user_a: str, user_b: str) -> Conversation:
    """Find existing DM or create one."""
    # Find conversation where both users are members
    subq_a = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user_a)
    subq_b = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user_b)

    result = await db.execute(
        select(Conversation).where(
            Conversation.type == ConversationType.DIRECT,
            Conversation.id.in_(subq_a),
            Conversation.id.in_(subq_b),
        ).limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    conv = Conversation(type=ConversationType.DIRECT)
    db.add(conv)
    await db.flush()

    for uid, role in [(user_a, MemberRole.MEMBER), (user_b, MemberRole.MEMBER)]:
        db.add(ConversationMember(conversation_id=conv.id, user_id=uid, role=role))

    return conv


@router.post("/direct", response_model=ConversationOut, status_code=201)
async def create_direct(
    data: CreateDirectConversation,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if data.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot create conversation with yourself")

    # Verify target user exists
    result = await db.execute(select(User).where(User.id == data.user_id, User.is_active == True))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    conv = await _get_or_create_direct(db, current_user.id, data.user_id)
    return await _load_conversation(db, conv.id, current_user.id)


@router.post("/group", response_model=ConversationOut, status_code=201)
async def create_group(
    data: CreateGroupConversation,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    all_members = list(set(data.member_ids + [current_user.id]))
    if len(all_members) > 256:
        raise HTTPException(status_code=400, detail="Group cannot exceed 256 members")

    conv = Conversation(
        type=ConversationType.GROUP,
        name=data.name,
        description=data.description,
        created_by=current_user.id,
    )
    db.add(conv)
    await db.flush()

    for uid in all_members:
        role = MemberRole.OWNER if uid == current_user.id else MemberRole.MEMBER
        db.add(ConversationMember(conversation_id=conv.id, user_id=uid, role=role))

    logger.info("Group created", group_id=conv.id, creator=current_user.id)
    return await _load_conversation(db, conv.id, current_user.id)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Conversation)
        .join(ConversationMember, and_(
            ConversationMember.conversation_id == Conversation.id,
            ConversationMember.user_id == current_user.id,
            ConversationMember.left_at == None,
        ))
        .order_by(Conversation.last_message_at.desc().nullslast())
        .options(selectinload(Conversation.members).selectinload(ConversationMember.user))
    )
    conversations = result.scalars().all()
    return conversations


@router.get("/{conv_id}", response_model=ConversationOut)
async def get_conversation(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_member(db, conv_id, current_user.id)
    return await _load_conversation(db, conv_id, current_user.id)


@router.patch("/{conv_id}", response_model=ConversationOut)
async def update_group(
    conv_id: str,
    data: UpdateGroup,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = await _assert_admin(db, conv_id, current_user.id)
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(conv, field, value)
    await db.flush()
    return await _load_conversation(db, conv_id, current_user.id)


@router.post("/{conv_id}/members", status_code=204)
async def add_members(
    conv_id: str,
    data: AddMembers,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_admin(db, conv_id, current_user.id)
    for uid in data.user_ids:
        result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conv_id,
                ConversationMember.user_id == uid,
            )
        )
        member = result.scalar_one_or_none()
        if member and member.left_at:
            member.left_at = None  # Re-add
        elif not member:
            db.add(ConversationMember(conversation_id=conv_id, user_id=uid))

    # Notify existing members
    await pubsub_manager.publish_to_conversation(conv_id, {
        "type": "members_added",
        "conversation_id": conv_id,
        "added_by": current_user.id,
        "user_ids": data.user_ids,
    })


@router.delete("/{conv_id}/members/{user_id}", status_code=204)
async def remove_member(
    conv_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Admins can remove others; anyone can remove themselves (leave)
    if user_id != current_user.id:
        await _assert_admin(db, conv_id, current_user.id)

    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conv_id,
            ConversationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    from datetime import datetime, timezone
    member.left_at = datetime.now(timezone.utc)

    await pubsub_manager.publish_to_conversation(conv_id, {
        "type": "member_removed",
        "conversation_id": conv_id,
        "removed_by": current_user.id,
        "user_id": user_id,
    })


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _assert_member(db: AsyncSession, conv_id: str, user_id: str) -> ConversationMember:
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


async def _assert_admin(db: AsyncSession, conv_id: str, user_id: str) -> Conversation:
    member = await _assert_member(db, conv_id, user_id)
    if member.role not in (MemberRole.OWNER, MemberRole.ADMIN):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    result = await db.execute(select(Conversation).where(Conversation.id == conv_id))
    return result.scalar_one()


async def _load_conversation(db: AsyncSession, conv_id: str, user_id: str) -> Conversation:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.id == conv_id)
        .options(selectinload(Conversation.members).selectinload(ConversationMember.user))
    )
    return result.scalar_one()
