"""
WebSocket integration tests.
Run with: pytest tests/test_websocket.py -v --asyncio-mode=auto
"""
import pytest
import pytest_asyncio
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_ws_typing_fanout():
    """Typing events should publish to Redis pub/sub."""
    from core.websocket_manager import ConnectionManager
    manager = ConnectionManager()

    ws_a = AsyncMock()
    ws_b = AsyncMock()

    await manager.connect("user_a", "conn_a", ws_a)
    await manager.connect("user_b", "conn_b", ws_b)

    typing_event = {
        "type": "typing",
        "user_id": "user_a",
        "conversation_id": "conv_1",
    }

    # Send to user_b
    delivered = await manager.send_to_user("user_b", typing_event)
    assert delivered is True
    ws_b.send_json.assert_called_once_with(typing_event)
    ws_a.send_json.assert_not_called()

    await manager.disconnect("user_a", "conn_a")
    await manager.disconnect("user_b", "conn_b")


@pytest.mark.asyncio
async def test_ws_broadcast():
    from core.websocket_manager import ConnectionManager
    manager = ConnectionManager()

    websockets = {}
    for uid in ("u1", "u2", "u3"):
        ws = AsyncMock()
        await manager.connect(uid, f"conn_{uid}", ws)
        websockets[uid] = ws

    payload = {"type": "system", "message": "Hello all"}
    await manager.broadcast_all(payload)

    for ws in websockets.values():
        ws.send_json.assert_called_once_with(payload)

    for uid, conn_id in [(u, f"conn_{u}") for u in ("u1", "u2", "u3")]:
        await manager.disconnect(uid, conn_id)


@pytest.mark.asyncio
async def test_ws_dead_connection_cleanup():
    """Dead WebSocket connections should be cleaned up on failed send."""
    from core.websocket_manager import ConnectionManager
    manager = ConnectionManager()

    dead_ws = AsyncMock()
    dead_ws.send_json.side_effect = Exception("Connection closed")

    await manager.connect("user_x", "conn_dead", dead_ws)
    assert manager.is_connected("user_x")

    result = await manager.send_to_user("user_x", {"type": "ping"})
    # After failure, connection should be cleaned up
    assert not manager.is_connected("user_x")
    assert result is False


@pytest.mark.asyncio
async def test_presence_manager_online_offline():
    """Test presence TTL logic with mocked Redis."""
    from core.redis_manager import PresenceManager
    manager = PresenceManager()

    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=1)
    mock_redis.setex = AsyncMock()
    mock_redis.delete = AsyncMock()
    mock_redis.srem = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.sadd = AsyncMock()

    import core.redis_manager as rm
    original = rm.redis_client
    rm.redis_client = mock_redis

    try:
        await manager.set_online("user1")
        mock_redis.setex.assert_called_once()

        is_online = await manager.is_online("user1")
        assert is_online is True

        await manager.set_offline("user1")
        mock_redis.delete.assert_called()
    finally:
        rm.redis_client = original
