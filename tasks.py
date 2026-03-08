"""
Celery tasks for async background processing:
- Push notifications (FCM/APNS)
- Email notifications
- Message cleanup
- Thumbnail generation
"""
from celery import Celery
from core.config import settings
import structlog

logger = structlog.get_logger()

celery_app = Celery(
    "messaging_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "tasks.send_push_notification": {"queue": "notifications"},
        "tasks.cleanup_old_messages": {"queue": "maintenance"},
    },
    beat_schedule={
        "cleanup-deleted-messages": {
            "task": "tasks.cleanup_old_messages",
            "schedule": 3600.0,  # Every hour
        },
        "cleanup-expired-tokens": {
            "task": "tasks.cleanup_expired_tokens",
            "schedule": 86400.0,  # Daily
        },
    },
)


@celery_app.task(name="tasks.send_push_notification", bind=True, max_retries=3)
def send_push_notification(self, user_id: str, title: str, body: str, data: dict = None):
    """Send FCM/APNS push notification to all user devices."""
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from models.models import PushToken

        engine = create_engine(settings.DATABASE_URL_SYNC)
        with Session(engine) as db:
            tokens = db.execute(
                select(PushToken).where(
                    PushToken.user_id == user_id,
                    PushToken.is_active == True,
                )
            ).scalars().all()

        for token in tokens:
            if token.platform in ("android", "web"):
                _send_fcm(token.token, title, body, data or {})
            elif token.platform == "ios":
                _send_apns(token.token, title, body, data or {})

        logger.info("Push notifications sent", user_id=user_id, count=len(tokens))
    except Exception as exc:
        logger.error("Push notification failed", user_id=user_id, error=str(exc))
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="tasks.cleanup_old_messages")
def cleanup_old_messages():
    """Hard-delete messages that were soft-deleted more than 30 days ago."""
    from sqlalchemy import create_engine, delete, text
    from sqlalchemy.orm import Session
    from models.models import Message

    engine = create_engine(settings.DATABASE_URL_SYNC)
    with Session(engine) as db:
        result = db.execute(
            delete(Message).where(
                text("deleted_at < NOW() - INTERVAL '30 days'")
            )
        )
        db.commit()
        logger.info("Cleaned up deleted messages", count=result.rowcount)


@celery_app.task(name="tasks.cleanup_expired_tokens")
def cleanup_expired_tokens():
    """Remove expired refresh tokens."""
    from sqlalchemy import create_engine, delete, text
    from sqlalchemy.orm import Session
    from models.models import RefreshToken

    engine = create_engine(settings.DATABASE_URL_SYNC)
    with Session(engine) as db:
        result = db.execute(
            delete(RefreshToken).where(
                text("expires_at < NOW() OR revoked = true")
            )
        )
        db.commit()
        logger.info("Cleaned up refresh tokens", count=result.rowcount)


@celery_app.task(name="tasks.send_message_notification")
def send_message_notification(sender_name: str, recipient_id: str, conversation_id: str, preview: str):
    """Trigger push notification for new message."""
    send_push_notification.delay(
        user_id=recipient_id,
        title=f"New message from {sender_name}",
        body=preview[:100],
        data={"conversation_id": conversation_id, "type": "new_message"},
    )


# ─── Platform integrations ───────────────────────────────────────────────────

def _send_fcm(token: str, title: str, body: str, data: dict):
    """Send via Firebase Cloud Messaging."""
    import requests
    # Replace with your FCM server key
    FCM_URL = "https://fcm.googleapis.com/fcm/send"
    headers = {
        "Authorization": f"key={settings.SECRET_KEY}",  # Use FCM_SERVER_KEY from settings
        "Content-Type": "application/json",
    }
    payload = {
        "to": token,
        "notification": {"title": title, "body": body},
        "data": data,
    }
    try:
        resp = requests.post(FCM_URL, json=payload, headers=headers, timeout=5)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("FCM send failed", error=str(e))


def _send_apns(token: str, title: str, body: str, data: dict):
    """Send via Apple Push Notification Service."""
    # Use httpx2 + JWT for APNS HTTP/2 — placeholder
    logger.info("APNS notification", token=token[:8], title=title)
