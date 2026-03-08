"""
Shared pytest fixtures for the entire test suite.
"""
import pytest
import pytest_asyncio
import asyncio
import os

# Set test environment before any app imports
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-characters-okk")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("REDIS_PUBSUB_URL", "redis://localhost:6379/15")
os.environ.setdefault("REDIS_CACHE_URL", "redis://localhost:6379/15")
os.environ.setdefault("DEBUG", "true")


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from models.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    Session = async_sessionmaker(test_engine, expire_on_commit=False)
    async with Session() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def http_client(db):
    """Async HTTP client with DB override."""
    from httpx import AsyncClient, ASGITransport
    from main import app
    from db.database import get_db

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def create_test_user(http_client, suffix: str = ""):
    """Helper to register + login a user and return tokens + user data."""
    username = f"testuser{suffix}"
    await http_client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "SecurePass1",
        "display_name": f"Test User {suffix}",
    })
    login = await http_client.post("/api/v1/auth/login", json={
        "username": username,
        "password": "SecurePass1",
    })
    tokens = login.json()
    me = await http_client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    return {"tokens": tokens, "user": me.json()}
