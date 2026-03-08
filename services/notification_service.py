"""
NotificationService: decides when and how to send push notifications.
Integrates with Celery tasks for async delivery.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.models import User, ConversationMember, PushToken
from core.redis_manager import presence_manager
import structlog

logger = structlog.get_logger()


class NotificationService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def notify_new_message(
        self,
        sender: User,
        conversation_id: str,
        member_ids: list[str],
        content_preview: str,
    ):
        """Send push notification to offline members who have push tokens."""
        from tasks import send_message_notification

        for uid in member_ids:
            if uid == sender.id:
                continue
            is_online = await presence_manager.is_online(uid)
            if is_online:
                continue  # They already got it via WebSocket

            # Check mute status
            result = await self.db.execute(
                select(ConversationMember).where(
                    ConversationMember.conversation_id == conversation_id,
                    ConversationMember.user_id == uid,
                )
            )
            member = result.scalar_one_or_none()
            if member and member.is_muted:
                from datetime import datetime, timezone
                if not member.muted_until or member.muted_until > datetime.now(timezone.utc):
                    continue

            send_message_notification.delay(
                sender_name=sender.display_name,
                recipient_id=uid,
                conversation_id=conversation_id,
                preview=content_preview or "Sent a file",
            )

    async def notify_added_to_group(self, user_id: str, group_name: str, added_by: str):
        from tasks import send_push_notification
        send_push_notification.delay(
            user_id=user_id,
            title=f"Added to {group_name}",
            body=f"{added_by} added you to a group",
            data={"type": "group_added"},
        )

    async def notify_missed_calls(self, caller_name: str, recipient_id: str):
        from tasks import send_push_notification
        send_push_notification.delay(
            user_id=recipient_id,
            title="Missed call",
            body=f"You missed a call from {caller_name}",
            data={"type": "missed_call"},
        )
