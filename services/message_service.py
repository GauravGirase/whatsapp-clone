"""
MessageService: encapsulates message creation, retrieval, and mutation logic.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from fastapi import HTTPException
from datetime import datetime, timezone
from models.models import (
    Message, MessageReceipt, MessageReaction,
    MessageStatus, MessageType, ConversationMember, Conversation
)
from core.redis_manager import pubsub_manager, pending_queue, presence_manager, cache_manager
from core.websocket_manager import ws_manager
import structlog

logger = structlog.get_logger()


class MessageService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ─── Fetch ────────────────────────────────────────────────────────────

    async def get_page(
        self,
        conversation_id: str,
        limit: int = 50,
        before_id: str | None = None,
    ) -> tuple[list[Message], bool]:
        """Returns (messages_asc, has_more)."""
        query = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.deleted_at == None,
            )
            .options(selectinload(Message.sender), selectinload(Message.reactions))
            .order_by(Message.created_at.desc())
            .limit(limit + 1)
        )
        if before_id:
            result = await self.db.execute(select(Message).where(Message.id == before_id))
            cursor = result.scalar_one_or_none()
            if cursor:
                query = query.where(Message.created_at < cursor.created_at)

        result = await self.db.execute(query)
        rows = result.scalars().all()
        has_more = len(rows) > limit
        return list(reversed(rows[:limit])), has_more

    async def get_by_id(self, message_id: str) -> Message | None:
        result = await self.db.execute(
            select(Message)
            .where(Message.id == message_id)
            .options(selectinload(Message.sender), selectinload(Message.reactions))
        )
        return result.scalar_one_or_none()

    # ─── Fanout helper ────────────────────────────────────────────────────

    async def fanout(self, member_ids: list[str], payload: dict, exclude: str | None = None):
        """
        Deliver a payload to all conversation members.
        Priority: local WebSocket → Redis pub/sub → offline queue
        """
        for uid in member_ids:
            if uid == exclude:
                continue
            delivered = await ws_manager.send_to_user(uid, payload)
            if not delivered:
                if await presence_manager.is_online(uid):
                    await pubsub_manager.publish_to_user(uid, payload)
                else:
                    await pending_queue.push(uid, payload)

    async def get_member_ids(self, conversation_id: str) -> list[str]:
        result = await self.db.execute(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.left_at == None,
            )
        )
        return [r[0] for r in result.all()]

    # ─── Mutations ────────────────────────────────────────────────────────

    async def soft_delete(self, message: Message) -> Message:
        message.deleted_at = datetime.now(timezone.utc)
        message.content = None
        message.type = MessageType.DELETED
        await self.db.flush()
        return message

    async def edit(self, message: Message, new_content: str) -> Message:
        if message.type != MessageType.TEXT:
            raise HTTPException(400, "Only text messages can be edited")
        message.content = new_content
        message.is_edited = True
        message.edited_at = datetime.now(timezone.utc)
        await self.db.flush()
        return message

    # ─── Receipts ─────────────────────────────────────────────────────────

    async def mark_delivered(self, message_id: str, user_id: str):
        await self._upsert_receipt(message_id, user_id, MessageStatus.DELIVERED)

    async def mark_read(self, message_id: str, user_id: str):
        await self._upsert_receipt(message_id, user_id, MessageStatus.READ)

    async def _upsert_receipt(self, message_id: str, user_id: str, status: MessageStatus):
        result = await self.db.execute(
            select(MessageReceipt).where(
                MessageReceipt.message_id == message_id,
                MessageReceipt.user_id == user_id,
            )
        )
        receipt = result.scalar_one_or_none()
        if receipt:
            receipt.status = status
        else:
            self.db.add(MessageReceipt(message_id=message_id, user_id=user_id, status=status))

    # ─── Reactions ────────────────────────────────────────────────────────

    async def toggle_reaction(self, message_id: str, user_id: str, emoji: str) -> str:
        """Returns 'added' or 'removed'."""
        result = await self.db.execute(
            select(MessageReaction).where(
                MessageReaction.message_id == message_id,
                MessageReaction.user_id == user_id,
                MessageReaction.emoji == emoji,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            await self.db.delete(existing)
            return "removed"
        self.db.add(MessageReaction(message_id=message_id, user_id=user_id, emoji=emoji))
        return "added"

    # ─── Conversation utils ───────────────────────────────────────────────

    async def bump_conversation(self, conversation_id: str):
        await self.db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_message_at=datetime.now(timezone.utc))
        )
        await cache_manager.delete_pattern(f"convlist:*")
