"""
Unit tests for service layer.
Run with: pytest tests/ -v --asyncio-mode=auto
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from utils.validators import sanitize_text, is_valid_uuid, truncate_preview, human_readable_size
from utils.pagination import encode_cursor, decode_cursor, make_message_cursor


# ─── Validator Tests ──────────────────────────────────────────────────────────

def test_sanitize_removes_null_bytes():
    assert "\x00" not in sanitize_text("hello\x00world")


def test_sanitize_allows_newlines():
    result = sanitize_text("line1\nline2")
    assert "\n" in result


def test_sanitize_truncates():
    long_text = "a" * 5000
    assert len(sanitize_text(long_text, max_length=100)) == 100


def test_is_valid_uuid_valid():
    import uuid
    assert is_valid_uuid(str(uuid.uuid4())) is True


def test_is_valid_uuid_invalid():
    assert is_valid_uuid("not-a-uuid") is False
    assert is_valid_uuid("") is False


def test_truncate_preview():
    assert truncate_preview("hello world", 5) == "hello…"
    assert truncate_preview("hi", 100) == "hi"
    assert truncate_preview(None) == ""


def test_human_readable_size():
    assert human_readable_size(500) == "500.0 B"
    assert human_readable_size(1024) == "1.0 KB"
    assert human_readable_size(1024 * 1024) == "1.0 MB"


# ─── Pagination Tests ─────────────────────────────────────────────────────────

def test_cursor_roundtrip():
    original = {"id": "abc-123", "ts": "2025-01-01T00:00:00"}
    encoded = encode_cursor(original)
    decoded = decode_cursor(encoded)
    assert decoded == original


def test_decode_invalid_cursor_returns_none():
    assert decode_cursor("not-valid-base64!!!") is None
    assert decode_cursor("") is None


def test_make_message_cursor():
    cursor = make_message_cursor("msg-id", "2025-01-01")
    assert cursor  # non-empty string
    decoded = decode_cursor(cursor)
    assert decoded["id"] == "msg-id"


# ─── WebSocket Manager Tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_manager_connect_disconnect():
    from core.websocket_manager import ConnectionManager
    manager = ConnectionManager()

    mock_ws = AsyncMock()
    await manager.connect("user1", "conn1", mock_ws)
    mock_ws.accept.assert_called_once()

    assert manager.is_connected("user1")
    assert manager.total_users == 1
    assert manager.total_connections == 1

    await manager.disconnect("user1", "conn1")
    assert not manager.is_connected("user1")
    assert manager.total_users == 0


@pytest.mark.asyncio
async def test_ws_manager_multi_device():
    from core.websocket_manager import ConnectionManager
    manager = ConnectionManager()

    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect("user1", "conn1", ws1)
    await manager.connect("user1", "conn2", ws2)

    assert manager.total_connections == 2
    assert manager.total_users == 1

    payload = {"type": "test", "data": "hello"}
    delivered = await manager.send_to_user("user1", payload)
    assert delivered is True
    ws1.send_json.assert_called_once_with(payload)
    ws2.send_json.assert_called_once_with(payload)


@pytest.mark.asyncio
async def test_ws_manager_send_to_offline_user():
    from core.websocket_manager import ConnectionManager
    manager = ConnectionManager()
    delivered = await manager.send_to_user("nonexistent", {"type": "test"})
    assert delivered is False


# ─── Auth Tests ───────────────────────────────────────────────────────────────

def test_password_hash_and_verify():
    from core.auth import hash_password, verify_password
    hashed = hash_password("SecurePass1")
    assert verify_password("SecurePass1", hashed) is True
    assert verify_password("WrongPass1", hashed) is False


def test_jwt_encode_decode():
    from core.auth import create_access_token, decode_access_token
    user_id = "user-abc-123"
    token = create_access_token(user_id)
    assert token
    decoded = decode_access_token(token)
    assert decoded == user_id


def test_jwt_invalid_token():
    from core.auth import decode_access_token
    assert decode_access_token("invalid.token.here") is None
    assert decode_access_token("") is None


# ─── Exception Tests ──────────────────────────────────────────────────────────

def test_custom_exceptions_have_correct_status():
    from utils.exceptions import NotFoundError, ForbiddenError, ConflictError, RateLimitError
    assert NotFoundError("User").status_code == 404
    assert ForbiddenError().status_code == 403
    assert ConflictError().status_code == 409
    assert RateLimitError().status_code == 429
