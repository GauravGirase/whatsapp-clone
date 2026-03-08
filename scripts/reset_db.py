#!/usr/bin/env python3
"""
Drop and recreate all tables. ⚠️ Destructive — dev only!
Usage: python scripts/reset_db.py
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine
from models.models import Base
from core.config import settings


async def reset():
    confirm = input("⚠️  This will DROP all tables. Type 'yes' to continue: ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    engine = create_async_engine(settings.DATABASE_URL, echo=True)
    async with engine.begin() as conn:
        print("Dropping all tables...")
        await conn.run_sync(Base.metadata.drop_all)
        print("Recreating all tables...")
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("✅ Database reset complete.")


if __name__ == "__main__":
    asyncio.run(reset())
