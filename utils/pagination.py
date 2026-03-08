"""
Cursor-based pagination utilities.
We use opaque base64 cursors to hide internals from clients.
"""
import base64
import json
from typing import Any


def encode_cursor(data: dict) -> str:
    """Encode a cursor dict into a URL-safe base64 string."""
    raw = json.dumps(data, default=str)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> dict | None:
    """Decode a cursor string back into a dict. Returns None if invalid."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        return json.loads(raw)
    except Exception:
        return None


def make_message_cursor(message_id: str, created_at: Any) -> str:
    return encode_cursor({"id": message_id, "ts": str(created_at)})


def parse_message_cursor(cursor: str) -> tuple[str | None, str | None]:
    """Returns (message_id, timestamp) or (None, None)."""
    data = decode_cursor(cursor)
    if not data:
        return None, None
    return data.get("id"), data.get("ts")
