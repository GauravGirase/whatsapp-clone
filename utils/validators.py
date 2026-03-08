"""
Shared validators, sanitizers, and small utility functions.
"""
import re
import uuid
import unicodedata
from datetime import datetime, timezone


# ─── Text ─────────────────────────────────────────────────────────────────────

def sanitize_text(text: str, max_length: int = 4096) -> str:
    """Strip control characters and truncate."""
    # Remove null bytes and control chars except \n \t
    cleaned = "".join(
        ch for ch in text
        if ch in ("\n", "\t") or not unicodedata.category(ch).startswith("C")
    )
    return cleaned[:max_length].strip()


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def truncate_preview(text: str | None, length: int = 100) -> str:
    """Produce a short message preview for notifications."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text[:length] + ("…" if len(text) > length else "")


# ─── Time ─────────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


# ─── Phone ────────────────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


def is_valid_phone(phone: str) -> bool:
    return bool(_PHONE_RE.match(phone.replace(" ", "").replace("-", "")))


# ─── Media ────────────────────────────────────────────────────────────────────

def human_readable_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes //= 1024
    return f"{size_bytes:.1f} TB"


MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "application/pdf": "pdf",
}


def ext_from_mime(mime: str) -> str:
    return MIME_TO_EXT.get(mime, "bin")
