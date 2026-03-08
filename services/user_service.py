"""
UserService: encapsulates all user-related business logic.
Keeps routers thin and business rules testable independently.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, update
from sqlalchemy.orm import selectinload
from fastapi import HTTPException
from datetime import datetime, timezone
from models.models import User, PushToken
from schemas.schemas import UserRegister, UserUpdate
from core.auth import hash_password, verify_password
from core.redis_manager import presence_manager, cache_manager
import structlog

logger = structlog.get_logger()

CACHE_TTL = 300  # 5 minutes


class UserService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ─── Read ─────────────────────────────────────────────────────────────

    async def get_by_id(self, user_id: str) -> User | None:
        cached = await cache_manager.get(f"user:{user_id}")
        if cached:
            # Re-hydrate as ORM object stub for simple reads
            user = User(**{k: v for k, v in cached.items() if k != "_cache"})
            return user

        result = await self.db.execute(
            select(User).where(User.id == user_id, User.is_active == True)
        )
        user = result.scalar_one_or_none()
        if user:
            await self._cache_user(user)
        return user

    async def get_by_username_or_email(self, identifier: str) -> User | None:
        result = await self.db.execute(
            select(User).where(
                or_(User.username == identifier, User.email == identifier),
                User.is_active == True,
            )
        )
        return result.scalar_one_or_none()

    async def search(self, query: str, requester_id: str, limit: int = 20) -> list[User]:
        result = await self.db.execute(
            select(User).where(
                or_(
                    User.username.ilike(f"%{query}%"),
                    User.display_name.ilike(f"%{query}%"),
                ),
                User.is_active == True,
                User.id != requester_id,
            ).limit(limit)
        )
        return result.scalars().all()

    # ─── Write ────────────────────────────────────────────────────────────

    async def create(self, data: UserRegister) -> User:
        existing = await self.db.execute(
            select(User).where(or_(User.username == data.username, User.email == data.email))
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username or email already taken")

        user = User(
            username=data.username,
            email=data.email,
            phone=data.phone,
            hashed_password=hash_password(data.password),
            display_name=data.display_name,
        )
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        logger.info("User created", user_id=user.id)
        return user

    async def update(self, user: User, data: UserUpdate) -> User:
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(user, field, value)
        await self.db.flush()
        await self.db.refresh(user)
        await cache_manager.delete(f"user:{user.id}")
        return user

    async def update_last_seen(self, user_id: str):
        await self.db.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_seen=datetime.now(timezone.utc))
        )
        await cache_manager.delete(f"user:{user_id}")

    async def deactivate(self, user: User):
        user.is_active = False
        await self.db.flush()
        await presence_manager.set_offline(user.id)
        await cache_manager.delete(f"user:{user.id}")
        logger.info("User deactivated", user_id=user.id)

    # ─── Push Tokens ──────────────────────────────────────────────────────

    async def register_push_token(self, user_id: str, token: str, platform: str, device_id: str | None):
        # Deactivate old tokens for this device
        if device_id:
            await self.db.execute(
                update(PushToken)
                .where(PushToken.user_id == user_id, PushToken.device_id == device_id)
                .values(is_active=False)
            )
        pt = PushToken(user_id=user_id, token=token, platform=platform, device_id=device_id)
        self.db.add(pt)
        await self.db.flush()

    async def get_push_tokens(self, user_id: str) -> list[PushToken]:
        result = await self.db.execute(
            select(PushToken).where(PushToken.user_id == user_id, PushToken.is_active == True)
        )
        return result.scalars().all()

    # ─── Helpers ──────────────────────────────────────────────────────────

    async def _cache_user(self, user: User):
        await cache_manager.set(f"user:{user.id}", {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "bio": user.bio,
            "is_verified": user.is_verified,
            "show_last_seen": user.show_last_seen,
            "last_seen": user.last_seen.isoformat() if user.last_seen else None,
        }, ttl=CACHE_TTL)
