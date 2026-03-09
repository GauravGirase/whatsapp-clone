from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from core.config import settings
from core.logging import setup_logging
from db.database import init_db, close_db
from core.redis_manager import init_redis, close_redis

from routers import auth, users, conversations, messages, websocket

import structlog

setup_logging()
logger = structlog.get_logger()


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up", app=settings.APP_NAME, version=settings.APP_VERSION)
    await init_redis()
    await init_db()
    yield
    logger.info("Shutting down")
    await close_redis()
    await close_db()


# ─── App ─────────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
## Real-time Messaging API

A production-grade WhatsApp-like messaging backend built with:
- **FastAPI** + **WebSockets** for real-time communication
- **PostgreSQL** + SQLAlchemy for persistent storage
- **Redis** for pub/sub, presence, caching, and offline queuing
- **Celery** for background tasks and push notifications
- **S3** for media storage

### WebSocket Events

Connect to `/ws?token=<jwt>` and send/receive JSON events.

**Inbound events:**
- `ping` → server responds with `pong`
- `typing_start` / `typing_stop` → `{data: {conversation_id}}`
- `read_receipt` → `{data: {message_ids: [...]}}`
- `presence_update` → `{data: {user_ids: [...]}}`

**Outbound events:**
- `new_message`, `message_updated`, `message_deleted`
- `typing`, `receipt_update`, `presence_update`, `pong`, `error`
    """,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── Middleware ───────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://13.233.200.143:8080"] if settings.DEBUG else ["https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)


# ─── Exception Handlers ──────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        errors.append({
            "field": " -> ".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
        })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation error", "errors": errors},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception", path=request.url.path, error=str(exc), exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ─── Routes ──────────────────────────────────────────────────────────────────

API_PREFIX = "/api/v1"

app.include_router(auth.router,          prefix=f"{API_PREFIX}")
app.include_router(users.router,         prefix=f"{API_PREFIX}")
app.include_router(conversations.router, prefix=f"{API_PREFIX}")
app.include_router(messages.router,      prefix=f"{API_PREFIX}")
app.include_router(websocket.router)     # /ws (no prefix — WebSocket)


# ─── Health & Metrics ────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health():
    from core.redis_manager import redis_client
    from db.database import engine
    checks = {}

    # Redis ping
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # DB ping
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        content={"status": "healthy" if all_ok else "degraded", "checks": checks},
        status_code=200 if all_ok else 503,
    )


@app.get("/metrics/ws", tags=["System"])
async def ws_metrics():
    from core.websocket_manager import ws_manager
    return {
        "active_connections": ws_manager.total_connections,
        "connected_users": ws_manager.total_users,
    }


# Prometheus metrics at /metrics
Instrumentator().instrument(app).expose(app)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        workers=1 if settings.DEBUG else 4,
        log_level="debug" if settings.DEBUG else "info",
    )
