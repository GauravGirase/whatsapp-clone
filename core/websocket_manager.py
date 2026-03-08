import asyncio
import json
from datetime import datetime, timezone
from fastapi import WebSocket
from typing import Optional
import structlog

logger = structlog.get_logger()


class ConnectionManager:
    """
    Manages active WebSocket connections on THIS server instance.
    Cross-server delivery is handled via Redis pub/sub.
    """

    def __init__(self):
        # user_id -> {connection_id: WebSocket}
        # A user may have multiple devices connected simultaneously
        self._connections: dict[str, dict[str, WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, connection_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            if user_id not in self._connections:
                self._connections[user_id] = {}
            self._connections[user_id][connection_id] = websocket
        logger.info("WebSocket connected", user_id=user_id, connection_id=connection_id)

    async def disconnect(self, user_id: str, connection_id: str):
        async with self._lock:
            if user_id in self._connections:
                self._connections[user_id].pop(connection_id, None)
                if not self._connections[user_id]:
                    del self._connections[user_id]
        logger.info("WebSocket disconnected", user_id=user_id, connection_id=connection_id)

    def is_connected(self, user_id: str) -> bool:
        return user_id in self._connections and bool(self._connections[user_id])

    def get_connected_users(self) -> list[str]:
        return list(self._connections.keys())

    async def send_to_user(self, user_id: str, payload: dict) -> bool:
        """Send to all devices of a user on THIS server. Returns True if delivered."""
        if user_id not in self._connections:
            return False

        delivered = False
        dead_connections = []

        for conn_id, ws in list(self._connections[user_id].items()):
            try:
                await ws.send_json(payload)
                delivered = True
            except Exception as e:
                logger.warning("Failed to send to WebSocket", user_id=user_id, error=str(e))
                dead_connections.append(conn_id)

        # Clean up dead connections
        for conn_id in dead_connections:
            await self.disconnect(user_id, conn_id)

        return delivered

    async def send_to_users(self, user_ids: list[str], payload: dict):
        """Broadcast to multiple users on this server."""
        tasks = [self.send_to_user(uid, payload) for uid in user_ids]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast_all(self, payload: dict):
        """Send to every connected user (e.g., system announcements)."""
        user_ids = self.get_connected_users()
        await self.send_to_users(user_ids, payload)

    @property
    def total_connections(self) -> int:
        return sum(len(conns) for conns in self._connections.values())

    @property
    def total_users(self) -> int:
        return len(self._connections)


# ─── Global singleton ────────────────────────────────────────────────────────

ws_manager = ConnectionManager()
