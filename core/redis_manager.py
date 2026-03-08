import json
import redis.asyncio as aioredis
from typing import Any, Optional
from core.config import settings
import structlog

logger = structlog.get_logger()

# ─── Redis Clients ───────────────────────────────────────────────────────────

# General purpose / Presence tracking
redis_client: aioredis.Redis = None

# Pub/Sub for message fanout
redis_pubsub: aioredis.Redis = None

# Cache
redis_cache: aioredis.Redis = None


async def init_redis():
    global redis_client, redis_pubsub, redis_cache
    redis_client = aioredis.from_url(
        settings.REDIS_URL, encoding="utf-8", decode_responses=True
    )
    redis_pubsub = aioredis.from_url(
        settings.REDIS_PUBSUB_URL, encoding="utf-8", decode_responses=True
    )
    redis_cache = aioredis.from_url(
        settings.REDIS_CACHE_URL, encoding="utf-8", decode_responses=True
    )
    logger.info("Redis connections initialized")


async def close_redis():
    if redis_client:
        await redis_client.aclose()
    if redis_pubsub:
        await redis_pubsub.aclose()
    if redis_cache:
        await redis_cache.aclose()
    logger.info("Redis connections closed")


# ─── Presence ────────────────────────────────────────────────────────────────

class PresenceManager:
    """Tracks online/offline status with TTL-based expiry."""

    PRESENCE_TTL = 60  # seconds; heartbeat must refresh before this expires
    PRESENCE_PREFIX = "presence:"

    async def set_online(self, user_id: str, server_id: str = "default"):
        key = f"{self.PRESENCE_PREFIX}{user_id}"
        await redis_client.setex(key, self.PRESENCE_TTL, server_id)
        await redis_client.sadd("online_users", user_id)

    async def set_offline(self, user_id: str):
        key = f"{self.PRESENCE_PREFIX}{user_id}"
        await redis_client.delete(key)
        await redis_client.srem("online_users", user_id)
        # Store last seen timestamp
        await redis_client.set(f"last_seen:{user_id}", _now_iso())

    async def is_online(self, user_id: str) -> bool:
        return await redis_client.exists(f"{self.PRESENCE_PREFIX}{user_id}") == 1

    async def get_online_users(self, user_ids: list[str]) -> dict[str, bool]:
        pipe = redis_client.pipeline()
        for uid in user_ids:
            pipe.exists(f"{self.PRESENCE_PREFIX}{uid}")
        results = await pipe.execute()
        return {uid: bool(r) for uid, r in zip(user_ids, results)}

    async def refresh(self, user_id: str):
        key = f"{self.PRESENCE_PREFIX}{user_id}"
        await redis_client.expire(key, self.PRESENCE_TTL)

    async def get_last_seen(self, user_id: str) -> Optional[str]:
        return await redis_client.get(f"last_seen:{user_id}")


# ─── Pub/Sub ─────────────────────────────────────────────────────────────────

class PubSubManager:
    """Routes messages between server instances via Redis pub/sub."""

    async def publish(self, channel: str, payload: dict):
        await redis_pubsub.publish(channel, json.dumps(payload))

    async def publish_to_user(self, user_id: str, payload: dict):
        await self.publish(f"user:{user_id}", payload)

    async def publish_to_conversation(self, conversation_id: str, payload: dict):
        await self.publish(f"conv:{conversation_id}", payload)

    def make_pubsub(self) -> aioredis.client.PubSub:
        return redis_pubsub.pubsub()


# ─── Cache ───────────────────────────────────────────────────────────────────

class CacheManager:
    """JSON-based cache with TTL."""

    async def get(self, key: str) -> Optional[Any]:
        val = await redis_cache.get(key)
        if val:
            return json.loads(val)
        return None

    async def set(self, key: str, value: Any, ttl: int = 300):
        await redis_cache.setex(key, ttl, json.dumps(value, default=str))

    async def delete(self, key: str):
        await redis_cache.delete(key)

    async def delete_pattern(self, pattern: str):
        keys = await redis_cache.keys(pattern)
        if keys:
            await redis_cache.delete(*keys)

    async def incr(self, key: str, ttl: int = 60) -> int:
        val = await redis_cache.incr(key)
        if val == 1:
            await redis_cache.expire(key, ttl)
        return val


# ─── Typing Indicators ───────────────────────────────────────────────────────

class TypingManager:
    """Ephemeral typing indicator state (Redis only, no DB)."""

    TYPING_TTL = 5  # seconds

    async def set_typing(self, conversation_id: str, user_id: str):
        key = f"typing:{conversation_id}"
        await redis_client.setex(f"{key}:{user_id}", self.TYPING_TTL, "1")
        # Publish event for real-time delivery
        await pubsub_manager.publish_to_conversation(conversation_id, {
            "type": "typing_start",
            "user_id": user_id,
            "conversation_id": conversation_id,
        })

    async def clear_typing(self, conversation_id: str, user_id: str):
        await redis_client.delete(f"typing:{conversation_id}:{user_id}")
        await pubsub_manager.publish_to_conversation(conversation_id, {
            "type": "typing_stop",
            "user_id": user_id,
            "conversation_id": conversation_id,
        })

    async def get_typing_users(self, conversation_id: str) -> list[str]:
        pattern = f"typing:{conversation_id}:*"
        keys = await redis_client.keys(pattern)
        return [k.split(":")[-1] for k in keys]


# ─── Rate Limiter ────────────────────────────────────────────────────────────

class RateLimiter:
    async def is_allowed(self, user_id: str, action: str, limit: int, window: int) -> bool:
        key = f"ratelimit:{action}:{user_id}"
        count = await cache_manager.incr(key, ttl=window)
        return count <= limit


# ─── Pending Message Queue ───────────────────────────────────────────────────

class PendingMessageQueue:
    """Store messages for offline users; deliver on reconnect."""

    async def push(self, user_id: str, message: dict):
        key = f"pending:{user_id}"
        await redis_client.lpush(key, json.dumps(message, default=str))
        # Keep at most 500 pending messages per user
        await redis_client.ltrim(key, 0, 499)

    async def flush(self, user_id: str) -> list[dict]:
        key = f"pending:{user_id}"
        messages = await redis_client.lrange(key, 0, -1)
        if messages:
            await redis_client.delete(key)
        return [json.loads(m) for m in reversed(messages)]


# ─── Singletons ──────────────────────────────────────────────────────────────

presence_manager  = PresenceManager()
pubsub_manager    = PubSubManager()
cache_manager     = CacheManager()
typing_manager    = TypingManager()
rate_limiter      = RateLimiter()
pending_queue     = PendingMessageQueue()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
