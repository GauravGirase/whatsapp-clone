"""
ConversationService: manages conversation creation, membership, and metadata.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from fastapi import HTTPException
from datetime import datetime, timezone
from models.models import (
    Conversation, ConversationMember, ConversationType, MemberRole, User
)
from core.redis_manager import pubsub_manager, cache_manager
import structlog

logger = structlog.get_logger()


class ConversationService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ─── Creation ─────────────────────────────────────────────────────────

    async def get_or_create_direct(self, user_a: str, user_b: str) -> Conversation:
        subq_a = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user_a)
        subq_b = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user_b)
        result = await self.db.execute(
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
        self.db.add(conv)
        await self.db.flush()
        for uid in (user_a, user_b):
            self.db.add(ConversationMember(conversation_id=conv.id, user_id=uid))
        logger.info("Direct conversation created", conv_id=conv.id, users=[user_a, user_b])
        return conv

    async def create_group(
        self,
        name: str,
        description: str | None,
        creator_id: str,
        member_ids: list[str],
    ) -> Conversation:
        all_ids = list(set(member_ids + [creator_id]))
        if len(all_ids) > 256:
            raise HTTPException(400, "Group cannot exceed 256 members")

        conv = Conversation(
            type=ConversationType.GROUP,
            name=name,
            description=description,
            created_by=creator_id,
        )
        self.db.add(conv)
        await self.db.flush()

        for uid in all_ids:
            role = MemberRole.OWNER if uid == creator_id else MemberRole.MEMBER
            self.db.add(ConversationMember(conversation_id=conv.id, user_id=uid, role=role))

        logger.info("Group created", conv_id=conv.id, creator=creator_id, members=len(all_ids))
        return conv

    # ─── Read ─────────────────────────────────────────────────────────────

    async def get_user_conversations(self, user_id: str) -> list[Conversation]:
        result = await self.db.execute(
            select(Conversation)
            .join(ConversationMember, and_(
                ConversationMember.conversation_id == Conversation.id,
                ConversationMember.user_id == user_id,
                ConversationMember.left_at == None,
            ))
            .options(selectinload(Conversation.members).selectinload(ConversationMember.user))
            .order_by(Conversation.last_message_at.desc().nullslast())
        )
        return result.scalars().all()

    async def get_with_members(self, conv_id: str) -> Conversation | None:
        result = await self.db.execute(
            select(Conversation)
            .where(Conversation.id == conv_id)
            .options(selectinload(Conversation.members).selectinload(ConversationMember.user))
        )
        return result.scalar_one_or_none()

    async def assert_member(self, conv_id: str, user_id: str) -> ConversationMember:
        result = await self.db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conv_id,
                ConversationMember.user_id == user_id,
                ConversationMember.left_at == None,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(403, "Not a member of this conversation")
        return member

    async def assert_admin(self, conv_id: str, user_id: str) -> ConversationMember:
        member = await self.assert_member(conv_id, user_id)
        if member.role not in (MemberRole.OWNER, MemberRole.ADMIN):
            raise HTTPException(403, "Admin privileges required")
        return member

    # ─── Membership ───────────────────────────────────────────────────────

    async def add_members(self, conv_id: str, user_ids: list[str], added_by: str):
        for uid in user_ids:
            result = await self.db.execute(
                select(ConversationMember).where(
                    ConversationMember.conversation_id == conv_id,
                    ConversationMember.user_id == uid,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.left_at = None
            else:
                self.db.add(ConversationMember(conversation_id=conv_id, user_id=uid))

        await pubsub_manager.publish_to_conversation(conv_id, {
            "type": "members_added",
            "conversation_id": conv_id,
            "added_by": added_by,
            "user_ids": user_ids,
        })

    async def remove_member(self, conv_id: str, user_id: str, removed_by: str):
        result = await self.db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conv_id,
                ConversationMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(404, "Member not found")
        member.left_at = datetime.now(timezone.utc)

        await pubsub_manager.publish_to_conversation(conv_id, {
            "type": "member_removed",
            "conversation_id": conv_id,
            "removed_by": removed_by,
            "user_id": user_id,
        })

    async def promote_member(self, conv_id: str, user_id: str, role: MemberRole):
        result = await self.db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conv_id,
                ConversationMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(404, "Member not found")
        member.role = role
        await self.db.flush()
