"""
WebSocket authentication and rate-limit middleware.
Validates JWT token before upgrading the connection.
"""
from fastapi import WebSocket, status
from core.auth import decode_access_token
from core.redis_manager import rate_limiter
import structlog

logger = structlog.get_logger()


async def ws_auth_middleware(websocket: WebSocket, token: str) -> str | None:
    """
    Validate a WebSocket connection's JWT token.
    Returns user_id on success, None on failure.
    """
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return None

    user_id = decode_access_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return None

    # Rate-limit: max 10 new WS connections per user per minute
    allowed = await rate_limiter.is_allowed(user_id, "ws_connect", limit=10, window=60)
    if not allowed:
        await websocket.close(code=4029, reason="Too many connections")
        return None

    return user_id
