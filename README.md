# 💬 Messaging API — FastAPI + Redis + PostgreSQL

A production-grade, WhatsApp-style real-time messaging backend.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
│  Mobile App    Web App    Another Device                         │
│     │              │            │                                │
│     └──────WebSocket────────────┘                                │
└──────────────────────┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────┐
│                    FastAPI Server(s)                              │
│                                                                  │
│  ┌──────────────┐  ┌─────────────────┐  ┌───────────────────┐  │
│  │  REST Routes  │  │  WebSocket /ws  │  │  Background Tasks  │  │
│  │  /auth        │  │  - Presence     │  │  (Celery)          │  │
│  │  /users       │  │  - Typing       │  │  - Push notifs     │  │
│  │  /convs       │  │  - Receipts     │  │  - Cleanup         │  │
│  │  /messages    │  │  - Events       │  │  - Thumbnails      │  │
│  └──────────────┘  └────────┬────────┘  └───────────────────┘  │
└───────────────────────────── │ ──────────────────────────────────┘
                               │
        ┌──────────────────────┼─────────────────────┐
        │                      │                      │
┌───────▼───────┐   ┌──────────▼──────────┐  ┌───────▼───────┐
│  PostgreSQL   │   │       Redis          │  │   S3 / MinIO  │
│               │   │                      │  │               │
│  - users      │   │  DB0: Presence/state │  │  - avatars    │
│  - messages   │   │  DB1: Pub/Sub fanout │  │  - images     │
│  - convs      │   │  DB2: Cache          │  │  - videos     │
│  - receipts   │   │  DB3: Celery broker  │  │  - files      │
│  - reactions  │   │  DB4: Celery results │  │               │
└───────────────┘   └──────────────────────┘  └───────────────┘
```

---

## ⚡ Quick Start

### 1. Start all services

```bash
cp .env.example .env
docker-compose up -d
```

### 2. Run the API directly (dev)

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### 3. API docs

- Swagger UI: http://localhost:8000/docs
- ReDoc:       http://localhost:8000/redoc
- Health:      http://localhost:8000/health
- Metrics:     http://localhost:8000/metrics
- WS metrics:  http://localhost:8000/metrics/ws
- Flower (Celery): http://localhost:5555
- Redis UI:    http://localhost:8081

---

## 📡 WebSocket Protocol

### Connect
```
ws://localhost:8000/ws?token=<JWT_ACCESS_TOKEN>
```

### Inbound Events (Client → Server)

```json
// Ping / keepalive
{ "type": "ping" }

// Typing indicators
{ "type": "typing_start", "data": { "conversation_id": "uuid" } }
{ "type": "typing_stop",  "data": { "conversation_id": "uuid" } }

// Mark messages read
{ "type": "read_receipt", "data": { "message_ids": ["uuid1", "uuid2"] } }

// Query presence
{ "type": "presence_update", "data": { "user_ids": ["uuid1", "uuid2"] } }
```

### Outbound Events (Server → Client)

```json
// New message
{
  "type": "new_message",
  "id": "msg-uuid",
  "conversation_id": "conv-uuid",
  "sender_id": "user-uuid",
  "message_type": "text",
  "content": "Hello!",
  "created_at": "2025-01-01T00:00:00Z"
}

// Typing indicator
{ "type": "typing", "user_id": "uuid", "conversation_id": "uuid" }

// Read receipt update
{ "type": "receipt_update", "message_id": "uuid", "read_by": "uuid", "status": "read" }

// Presence change
{ "type": "presence_update", "presence": [{ "user_id": "uuid", "is_online": true }] }

// Heartbeat response
{ "type": "pong", "ts": "2025-01-01T00:00:00Z" }
```

---

## 🔑 REST API Examples

### Register & Login
```bash
# Register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","email":"alice@example.com","password":"SecurePass1","display_name":"Alice"}'

# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"SecurePass1"}'
```
### Direct communication
```bash
curl -X POST http://43.205.146.139:8000/api/v1/conversations/direct -H "Authorization: Bearer ****" -H "Content-Type: application/json" -d '{"user_id": "f5fbd80d-3f7c-40a5-8054-de5d3c7ac0cd"}'
```
## Get conversations
```bash
curl http://43.205.146.139:8000/api/v1/conversations   -H "Authorization: Bearer ***"
```

### Send a message
```bash
curl -X POST http://43.205.146.139:8000/api/v1/messages \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJiMTQ1MGVjNC0zNDNkLTRiZDAtYjEyOC1iOTczNDZlMzRhNTMiLCJleHAiOjE3NzMwNDQwNzYsInR5cGUiOiJhY2Nlc3MifQ.v9d0pT3q4tQIfELZI4FZqnRplj6kmLnWWR2Z-UbKQB0" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"0fec3805-52dc-4671-9b6b-c73c20553a31","type":"text","content":"Hey there!"}'
```

### Get messages with pagination
```bash
# First page
curl http://localhost:8000/api/v1/messages/<conv_id>?limit=50 \
  -H "Authorization: Bearer <token>"

# Next page (cursor-based)
curl "http://localhost:8000/api/v1/messages/<conv_id>?limit=50&before=<message_id>" \
  -H "Authorization: Bearer <token>"
```

---

## 📁 Project Structure

```
messaging_api/
├── main.py                         # FastAPI app, middleware, lifespan, health
├── tasks.py                        # Celery background tasks (push, cleanup)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml              # Postgres, Redis, MinIO, Flower, Redis-UI
├── alembic.ini                     # Alembic config
├── pytest.ini                      # Pytest config
├── .env.example
├── .gitignore
│
├── core/
│   ├── config.py                   # Settings via pydantic-settings + .env
│   ├── auth.py                     # JWT, bcrypt, OAuth2 dependencies
│   ├── redis_manager.py            # Presence, pub/sub, cache, typing, rate limiter, offline queue
│   ├── websocket_manager.py        # Multi-device WebSocket connection registry
│   ├── storage.py                  # S3/MinIO upload + Pillow thumbnail generation
│   └── logging.py                  # structlog JSON/console setup
│
├── models/
│   └── models.py                   # All SQLAlchemy ORM models (9 tables + enums)
│
├── schemas/
│   └── schemas.py                  # Pydantic v2 request/response models + WS event types
│
├── db/
│   └── database.py                 # Async engine, session factory, init/close
│
├── routers/
│   ├── auth.py                     # /auth/register, /login, /refresh, /logout
│   ├── users.py                    # /users/me, search, avatar upload, bulk presence
│   ├── conversations.py            # Direct + group CRUD, membership management
│   ├── messages.py                 # Send, paginate, edit, delete, forward, react, receipts
│   └── websocket.py                # WS /ws — real-time hub with Redis listener + heartbeat
│
├── services/
│   ├── user_service.py             # User CRUD + cache + push token management
│   ├── message_service.py          # Message CRUD + fanout + receipts + reactions
│   ├── conversation_service.py     # Conversation creation + membership logic
│   └── notification_service.py     # Push notification routing (online/offline/muted)
│
├── dependencies/
│   └── deps.py                     # Reusable FastAPI deps: pagination, rate limits, membership
│
├── middleware/
│   ├── request_middleware.py       # RequestID, structured logging, security headers
│   └── ws_middleware.py            # WebSocket auth + rate limit guard
│
├── utils/
│   ├── pagination.py               # Base64 cursor encoding/decoding
│   ├── validators.py               # Text sanitize, UUID check, phone, preview, size
│   └── exceptions.py              # Custom HTTP exception classes
│
├── migrations/
│   ├── env.py                      # Alembic async migration environment
│   └── versions/
│       ├── 001_initial.py          # Create all 9 tables
│       └── 002_indexes.py          # Performance + FTS indexes
│
├── nginx/
│   └── nginx.conf                  # Reverse proxy, WS upgrade, rate limit, SSL
│
├── scripts/
│   ├── seed_db.py                  # Create demo users, convs, messages
│   ├── reset_db.py                 # Drop + recreate all tables (dev only)
│   └── ws_client.py                # Interactive WebSocket REPL for manual testing
│
└── tests/
    ├── conftest.py                 # Shared fixtures: engine, db session, http client
    ├── test_api.py                 # Integration tests: auth, users, conversations
    ├── test_services.py            # Unit tests: validators, pagination, auth, WS manager
    └── test_websocket.py           # WebSocket-specific tests: typing, broadcast, cleanup
```

---

## 🚀 Scaling Notes

| Concern | Solution |
|---------|----------|
| Multiple API servers | Redis pub/sub routes messages across instances |
| Offline users | Messages queued in Redis, flushed on reconnect |
| Large groups (256+) | Fan-out via Redis channel per conversation |
| High message volume | PostgreSQL with connection pooling (asyncpg) |
| Media files | S3/MinIO — never stored in DB |
| Background work | Celery workers (push notifs, cleanup) |
| Rate limiting | slowapi (per-IP) + Redis-based per-user limits |
| Observability | structlog (JSON), Prometheus metrics, /health |

---

## 🧪 Running Tests

```bash
pip install pytest pytest-asyncio httpx aiosqlite
pytest tests/ -v --asyncio-mode=auto
```

---

## 🔒 Security Checklist

- [x] Bcrypt password hashing
- [x] JWT access tokens (short-lived, 24h default)
- [x] Refresh token rotation (one-time use)
- [x] Rate limiting on auth endpoints
- [x] Input validation via Pydantic v2
- [x] CORS configured per environment
- [x] SQL injection prevention (SQLAlchemy ORM)
- [ ] HTTPS / TLS termination (handled by nginx/load balancer)
- [ ] FCM server key in env (not hardcoded)



eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmNWZiZDgwZC0zZjdjLTQwYTUtODA1NC1kZTVkM2M3YWMwY2QiLCJleHAiOjE3NzMwNDMyNjgsInR5cGUiOiJhY2Nlc3MifQ.7-7ITFb3d6jPBIyFjUSTmU4EBNRqdAZxV9jWUpXCrok