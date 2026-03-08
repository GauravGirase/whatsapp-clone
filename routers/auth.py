from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from datetime import datetime, timedelta, timezone
from db.database import get_db
from models.models import User, RefreshToken
from schemas.schemas import UserRegister, UserLogin, TokenResponse, RefreshRequest, UserOut
from core.auth import hash_password, verify_password, create_access_token, create_refresh_token
from core.config import settings
from core.redis_manager import rate_limiter
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    # Check uniqueness
    result = await db.execute(
        select(User).where(or_(User.username == data.username, User.email == data.email))
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username or email already taken")

    user = User(
        username=data.username,
        email=data.email,
        phone=data.phone,
        hashed_password=hash_password(data.password),
        display_name=data.display_name,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    logger.info("User registered", user_id=user.id)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, request: Request, db: AsyncSession = Depends(get_db)):
    # Rate limit: 10 attempts per minute per IP
    ip = request.client.host
    if not await rate_limiter.is_allowed(ip, "login", limit=10, window=60):
        raise HTTPException(status_code=429, detail="Too many login attempts")

    # Look up by username or email
    result = await db.execute(
        select(User).where(
            or_(User.username == data.username, User.email == data.username),
            User.is_active == True
        )
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token(user.id)
    raw_refresh = create_refresh_token()

    rt = RefreshToken(
        user_id=user.id,
        token=raw_refresh,
        device_id=data.device_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(rt)

    # Update last seen
    user.last_seen = datetime.now(timezone.utc)

    logger.info("User logged in", user_id=user.id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token == data.refresh_token,
            RefreshToken.revoked == False,
            RefreshToken.expires_at > datetime.now(timezone.utc),
        )
    )
    rt = result.scalar_one_or_none()
    if not rt:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    # Rotate: revoke old, issue new
    rt.revoked = True
    new_refresh = create_refresh_token()
    new_rt = RefreshToken(
        user_id=rt.user_id,
        token=new_refresh,
        device_id=rt.device_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(new_rt)

    return TokenResponse(
        access_token=create_access_token(rt.user_id),
        refresh_token=new_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", status_code=204)
async def logout(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token == data.refresh_token)
    )
    rt = result.scalar_one_or_none()
    if rt:
        rt.revoked = True
