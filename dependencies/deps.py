"""
Reusable FastAPI dependency functions.
Import these in route handlers to DRY up auth, pagination, etc.
"""
from fastapi import Depends, HTTPException, Query, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from db.database import get_db
from models.models import User, Conversation, ConversationMember
from core.auth import get_current_user
from core.redis_manager import rate_limiter
from core.config import settings
import structlog

logger = structlog.get_logger()


# ─── Pagination ──────────────────────────────────────────────────────────────

class PaginationDep:
    def __init__(
        self,
        limit: int = Query(50, ge=1, le=100, description="Number of items per page"),
        before: Optional[str] = Query(None, description="Cursor: ID of last item seen"),
        after: Optional[str] = Query(None, description="Cursor: ID of first item seen"),
    ):
        self.limit = limit
        self.before = before
        self.after = after


# ─── Verified user ───────────────────────────────────────────────────────────

async def get_verified_user(current_user: User = Depends(get_current_user)) -> User:
    """Require the user's email to be verified."""
    if not current_user.is_verified:
        raise HTTPException(status_code=403, detail="Email verification required")
    return current_user


# ─── Conversation membership ─────────────────────────────────────────────────

class ConversationMemberDep:
    """
    Resolves + validates conversation membership.
    Usage:
        @router.get("/{conv_id}/...")
        async def handler(membership = Depends(ConversationMemberDep())):
    """
    async def __call__(
        self,
        conv_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ) -> ConversationMember:
        result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conv_id,
                ConversationMember.user_id == current_user.id,
                ConversationMember.left_at == None,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=403, detail="Not a member of this conversation")
        return member


# ─── Rate limiting dependency ─────────────────────────────────────────────────

def make_rate_limit_dep(action: str, limit: int, window: int):
    """
    Factory that creates a rate-limit dependency.
    Usage:
        rate_limited = Depends(make_rate_limit_dep("send_message", 30, 60))
    """
    async def _dep(current_user: User = Depends(get_current_user)):
        allowed = await rate_limiter.is_allowed(current_user.id, action, limit, window)
        if not allowed:
            raise HTTPException(status_code=429, detail=f"Rate limit exceeded for {action}")
    return _dep


# ─── API Version header ───────────────────────────────────────────────────────

async def require_api_version(
    x_api_version: Optional[str] = Header(None, alias="X-API-Version")
):
    """
    Optionally enforce that clients send an API version header.
    Soft-check: warns but does not reject unknown versions.
    """
    if x_api_version and x_api_version not in ("1", "1.0"):
        logger.warning("Unsupported API version requested", version=x_api_version)
    return x_api_version


# Convenience pre-built rate limit dependencies
message_rate_limit  = make_rate_limit_dep("send_message", limit=30, window=60)
search_rate_limit   = make_rate_limit_dep("search", limit=20, window=60)
upload_rate_limit   = make_rate_limit_dep("upload", limit=10, window=60)
