import asyncio
import json
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.database import AsyncSessionLocal
from models.models import ConversationMember, Message, MessageStatus
from schemas.schemas import WSEventType
from core.websocket_manager import ws_manager
from core.redis_manager import (
    presence_manager, pubsub_manager, typing_manager,
    pending_queue, rate_limiter
)
from core.auth import get_current_user_ws
from core.config import settings
import structlog

logger = structlog.get_logger()
router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    # ─── Authenticate ───────────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        user = await get_current_user_ws(token, db)

    if not user:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    user_id = user.id
    connection_id = str(uuid.uuid4())

    # ─── Connect ────────────────────────────────────────────────────────────
    await ws_manager.connect(user_id, connection_id, websocket)
    await presence_manager.set_online(user_id)

    # Deliver pending messages (sent while user was offline)
    pending = await pending_queue.flush(user_id)
    for msg in pending:
        try:
            await websocket.send_json(msg)
        except Exception:
            pass

    # Subscribe to personal Redis channel (for cross-server delivery)
    pubsub = pubsub_manager.make_pubsub()
    await pubsub.subscribe(f"user:{user_id}")

    # ─── Background tasks ───────────────────────────────────────────────────

    async def redis_listener():
        """Forward messages published to this user's Redis channel → WebSocket."""
        try:
            async for raw in pubsub.listen():
                if raw["type"] == "message":
                    data = json.loads(raw["data"])
                    try:
                        await websocket.send_json(data)
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Redis listener error", user_id=user_id, error=str(e))

    async def heartbeat():
        """Send ping every N seconds; refresh presence TTL."""
        try:
            while True:
                await asyncio.sleep(settings.WEBSOCKET_HEARTBEAT_INTERVAL)
                await websocket.send_json({"type": WSEventType.PONG, "ts": _now()})
                await presence_manager.refresh(user_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    redis_task = asyncio.create_task(redis_listener())
    heartbeat_task = asyncio.create_task(heartbeat())

    logger.info("WebSocket session started", user_id=user_id, connection_id=connection_id)

    # ─── Main receive loop ──────────────────────────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": WSEventType.ERROR, "detail": "Invalid JSON"})
                continue

            await handle_event(user_id, event, websocket)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected", user_id=user_id)
    except Exception as e:
        logger.error("WebSocket error", user_id=user_id, error=str(e))
    finally:
        # ─── Cleanup ────────────────────────────────────────────────────────
        redis_task.cancel()
        heartbeat_task.cancel()
        await pubsub.unsubscribe(f"user:{user_id}")
        await pubsub.aclose()
        await ws_manager.disconnect(user_id, connection_id)

        # Only mark offline if no other connections remain for this user
        if not ws_manager.is_connected(user_id):
            await presence_manager.set_offline(user_id)
            # Broadcast offline presence to contacts
            await _broadcast_presence(user_id, is_online=False)

        logger.info("WebSocket cleanup complete", user_id=user_id)


# ─── Event Handlers ──────────────────────────────────────────────────────────

async def handle_event(user_id: str, event: dict, websocket: WebSocket):
    event_type = event.get("type")
    data = event.get("data", {})

    if event_type == WSEventType.PING:
        await websocket.send_json({"type": WSEventType.PONG, "ts": _now()})

    

    elif event_type == WSEventType.TYPING_START:
        conv_id = data.get("conversation_id")
        if conv_id and await _is_member(user_id, conv_id):
            if not await rate_limiter.is_allowed(user_id, f"typing:{conv_id}", limit=10, window=5):
                return
            await typing_manager.set_typing(conv_id, user_id)

    elif event_type == WSEventType.TYPING_STOP:
        conv_id = data.get("conversation_id")
        if conv_id:
            await typing_manager.clear_typing(conv_id, user_id)

    elif event_type == WSEventType.READ_RECEIPT:
        message_ids = data.get("message_ids", [])
        if not message_ids:
            return
        async with AsyncSessionLocal() as db:
            from models.models import MessageReceipt
            for msg_id in message_ids[:50]:  # Cap at 50
                existing = await db.execute(
                    select(MessageReceipt).where(
                        MessageReceipt.message_id == msg_id,
                        MessageReceipt.user_id == user_id,
                    )
                )
                receipt = existing.scalar_one_or_none()
                if receipt:
                    receipt.status = MessageStatus.READ
                else:
                    db.add(MessageReceipt(
                        message_id=msg_id,
                        user_id=user_id,
                        status=MessageStatus.READ,
                    ))
            await db.commit()

        # Notify senders
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Message.id, Message.sender_id).where(Message.id.in_(message_ids))
            )
            for msg_id, sender_id in result.all():
                await pubsub_manager.publish_to_user(sender_id, {
                    "type": WSEventType.RECEIPT_UPDATE,
                    "message_id": msg_id,
                    "read_by": user_id,
                    "status": "read",
                })

    elif event_type == WSEventType.PRESENCE_UPDATE:
        # Client requesting presence for a list of users
        user_ids = data.get("user_ids", [])[:50]
        online_map = await presence_manager.get_online_users(user_ids)
        await websocket.send_json({
            "type": WSEventType.PRESENCE_UPDATE,
            "presence": [
                {"user_id": uid, "is_online": online}
                for uid, online in online_map.items()
            ],
        })

    else:
        await websocket.send_json({
            "type": WSEventType.ERROR,
            "detail": f"Unknown event type: {event_type}"
        })


# ─── Utilities ───────────────────────────────────────────────────────────────

async def _is_member(user_id: str, conv_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conv_id,
                ConversationMember.user_id == user_id,
                ConversationMember.left_at == None,
            )
        )
        return result.scalar_one_or_none() is not None


async def _broadcast_presence(user_id: str, is_online: bool):
    """Notify all conversation partners of presence change."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ConversationMember.user_id)
            .join(ConversationMember, ConversationMember.conversation_id ==
                  select(ConversationMember.conversation_id)
                  .where(ConversationMember.user_id == user_id)
                  .scalar_subquery())
            .where(ConversationMember.user_id != user_id)
            .distinct()
        )
        contact_ids = [r[0] for r in result.all()]

    payload = {
        "type": WSEventType.PRESENCE_UPDATE,
        "presence": [{"user_id": user_id, "is_online": is_online}],
    }
    for cid in contact_ids:
        await pubsub_manager.publish_to_user(cid, payload)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
