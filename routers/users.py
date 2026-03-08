from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from db.database import get_db
from models.models import User
from schemas.schemas import UserOut, UserUpdate, PresenceOut
from core.auth import get_current_user, hash_password
from core.redis_manager import presence_manager
from core.storage import upload_avatar
from datetime import datetime, timezone

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserOut)
async def update_me(
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(current_user, field, value)
    await db.flush()
    await db.refresh(current_user)
    return current_user


@router.post("/me/avatar", response_model=UserOut)
async def upload_avatar_endpoint(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WEBP allowed")
    url = await upload_avatar(file, current_user.id)
    current_user.avatar_url = url
    await db.flush()
    return current_user


@router.get("/search", response_model=list[UserOut])
async def search_users(
    q: str = Query(..., min_length=2),
    limit: int = Query(20, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(User).where(
            or_(
                User.username.ilike(f"%{q}%"),
                User.display_name.ilike(f"%{q}%"),
            ),
            User.is_active == True,
            User.id != current_user.id,
        ).limit(limit)
    )
    return result.scalars().all()


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/presence", response_model=list[PresenceOut])
async def get_presence(
    user_ids: list[str],
    current_user: User = Depends(get_current_user),
):
    """Bulk presence check for a list of user IDs."""
    online_map = await presence_manager.get_online_users(user_ids)
    result = []
    for uid in user_ids:
        last_seen = None
        if not online_map.get(uid):
            last_seen = await presence_manager.get_last_seen(uid)
        result.append(PresenceOut(user_id=uid, is_online=online_map.get(uid, False), last_seen=last_seen))
    return result
