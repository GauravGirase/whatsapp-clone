#!/usr/bin/env python3
"""
Seed script: creates demo users, conversations, and messages for local development.
Usage: python scripts/seed_db.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from models.models import Base, User, Conversation, ConversationMember, Message, ConversationType, MessageStatus
from core.auth import hash_password
from core.config import settings
import uuid


async def seed():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as db:
        print("🌱 Seeding users...")

        users_data = [
            ("alice",  "alice@example.com",  "Alice Smith",   "+1234567890"),
            ("bob",    "bob@example.com",    "Bob Jones",     "+1234567891"),
            ("charlie","charlie@example.com","Charlie Brown", "+1234567892"),
            ("diana",  "diana@example.com",  "Diana Prince",  "+1234567893"),
            ("eve",    "eve@example.com",    "Eve Wilson",    "+1234567894"),
        ]

        users = []
        for username, email, display_name, phone in users_data:
            u = User(
                id=str(uuid.uuid4()),
                username=username,
                email=email,
                phone=phone,
                display_name=display_name,
                hashed_password=hash_password("SecurePass1"),
                is_active=True,
                is_verified=True,
            )
            db.add(u)
            users.append(u)

        await db.flush()
        print(f"  ✓ Created {len(users)} users")

        # ── Direct conversation: alice ↔ bob ──────────────────────────────
        print("🌱 Seeding conversations...")
        dm = Conversation(id=str(uuid.uuid4()), type=ConversationType.DIRECT)
        db.add(dm)
        await db.flush()

        for user in users[:2]:
            db.add(ConversationMember(conversation_id=dm.id, user_id=user.id))

        # ── Group conversation ────────────────────────────────────────────
        from models.models import MemberRole
        group = Conversation(
            id=str(uuid.uuid4()),
            type=ConversationType.GROUP,
            name="The Squad 🎉",
            description="Demo group chat",
            created_by=users[0].id,
        )
        db.add(group)
        await db.flush()

        for i, user in enumerate(users):
            role = MemberRole.OWNER if i == 0 else MemberRole.MEMBER
            db.add(ConversationMember(conversation_id=group.id, user_id=user.id, role=role))

        print(f"  ✓ Created 1 DM, 1 group")

        # ── Messages ──────────────────────────────────────────────────────
        print("🌱 Seeding messages...")

        sample_messages = [
            (users[0].id, "Hey Bob, how's it going?"),
            (users[1].id, "Hey Alice! Doing great, thanks. You?"),
            (users[0].id, "Pretty good! Want to catch up later?"),
            (users[1].id, "Absolutely, sounds good 👍"),
        ]

        for sender_id, content in sample_messages:
            msg = Message(
                id=str(uuid.uuid4()),
                conversation_id=dm.id,
                sender_id=sender_id,
                content=content,
                status=MessageStatus.READ,
            )
            db.add(msg)

        group_messages = [
            (users[0].id, "Welcome everyone! 🎉"),
            (users[1].id, "Thanks for adding me!"),
            (users[2].id, "This is awesome"),
            (users[3].id, "Hey all 👋"),
            (users[4].id, "Hi team!"),
        ]

        for sender_id, content in group_messages:
            msg = Message(
                id=str(uuid.uuid4()),
                conversation_id=group.id,
                sender_id=sender_id,
                content=content,
                status=MessageStatus.READ,
            )
            db.add(msg)

        await db.commit()
        print(f"  ✓ Created {len(sample_messages) + len(group_messages)} messages")

    await engine.dispose()
    print("\n✅ Seed complete!")
    print("\nLogin credentials (all use password: SecurePass1):")
    for username, email, _, _ in users_data:
        print(f"  {username} / {email}")


if __name__ == "__main__":
    asyncio.run(seed())
