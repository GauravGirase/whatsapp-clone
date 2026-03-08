"""
Integration tests for the Messaging API.
Run with: pytest tests/ -v --asyncio-mode=auto
"""
import pytest
import pytest_asyncio
import asyncio
import json
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Use in-memory SQLite for tests
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

import os
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["SECRET_KEY"] = "test-secret-key-32-characters-ok"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["DEBUG"] = "true"

from main import app
from db.database import get_db
from models.models import Base


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session")
async def db_engine():
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    TestSession = async_sessionmaker(db_engine, expire_on_commit=False)
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session):
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ─── Auth Tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_user(client):
    resp = await client.post("/api/v1/auth/register", json={
        "username": "testuser",
        "email": "test@example.com",
        "password": "SecurePass1",
        "display_name": "Test User",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "testuser"
    assert "id" in data


@pytest.mark.asyncio
async def test_register_duplicate_username(client):
    payload = {
        "username": "dupeuser",
        "email": "dupe@example.com",
        "password": "SecurePass1",
        "display_name": "Dupe",
    }
    await client.post("/api/v1/auth/register", json=payload)
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login(client):
    await client.post("/api/v1/auth/register", json={
        "username": "loginuser",
        "email": "login@example.com",
        "password": "SecurePass1",
        "display_name": "Login User",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "username": "loginuser",
        "password": "SecurePass1",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    resp = await client.post("/api/v1/auth/login", json={
        "username": "loginuser",
        "password": "WrongPassword1",
    })
    assert resp.status_code == 401


# ─── User Tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_me(client):
    # Register and login
    await client.post("/api/v1/auth/register", json={
        "username": "meuser",
        "email": "me@example.com",
        "password": "SecurePass1",
        "display_name": "Me User",
    })
    login = await client.post("/api/v1/auth/login", json={
        "username": "meuser", "password": "SecurePass1"
    })
    token = login.json()["access_token"]

    resp = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "meuser"


@pytest.mark.asyncio
async def test_search_users(client):
    # Register two users
    for i in range(2):
        await client.post("/api/v1/auth/register", json={
            "username": f"searchuser{i}",
            "email": f"search{i}@example.com",
            "password": "SecurePass1",
            "display_name": f"Search User {i}",
        })
    login = await client.post("/api/v1/auth/login", json={
        "username": "searchuser0", "password": "SecurePass1"
    })
    token = login.json()["access_token"]

    resp = await client.get(
        "/api/v1/users/search?q=search",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


# ─── Conversation Tests ───────────────────────────────────────────────────────

async def _register_and_login(client, suffix: str):
    await client.post("/api/v1/auth/register", json={
        "username": f"convuser{suffix}",
        "email": f"conv{suffix}@example.com",
        "password": "SecurePass1",
        "display_name": f"Conv User {suffix}",
    })
    login = await client.post("/api/v1/auth/login", json={
        "username": f"convuser{suffix}", "password": "SecurePass1"
    })
    return login.json()


@pytest.mark.asyncio
async def test_create_direct_conversation(client):
    tokens_a = await _register_and_login(client, "a1")
    tokens_b = await _register_and_login(client, "b1")

    # Get user B's ID
    me_b = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {tokens_b['access_token']}"}
    )
    user_b_id = me_b.json()["id"]

    resp = await client.post(
        "/api/v1/conversations/direct",
        json={"user_id": user_b_id},
        headers={"Authorization": f"Bearer {tokens_a['access_token']}"},
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "direct"


@pytest.mark.asyncio
async def test_create_group_conversation(client):
    tokens_a = await _register_and_login(client, "g1")
    tokens_b = await _register_and_login(client, "g2")

    me_b = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {tokens_b['access_token']}"}
    )

    resp = await client.post(
        "/api/v1/conversations/group",
        json={"name": "Test Group", "member_ids": [me_b.json()["id"]]},
        headers={"Authorization": f"Bearer {tokens_a['access_token']}"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "Test Group"
    assert resp.json()["type"] == "group"


# ─── Health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    # May be degraded in test env (no real Redis), but endpoint should respond
    assert resp.status_code in (200, 503)
    assert "status" in resp.json()
