import json
from typing import Any
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        self._connections.setdefault(user_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: int):
        conns = self._connections.get(user_id, [])
        if websocket in conns:
            conns.remove(websocket)

    async def send_to_user(self, user_id: int, data: dict[str, Any]):
        message = json.dumps(data)
        for ws in list(self._connections.get(user_id, [])):
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect(ws, user_id)

    async def send_to_users(self, user_ids: list[int], data: dict[str, Any]):
        for user_id in user_ids:
            await self.send_to_user(user_id, data)

    async def broadcast(self, data: dict[str, Any]):
        message = json.dumps(data)
        for conns in list(self._connections.values()):
            for ws in list(conns):
                try:
                    await ws.send_text(message)
                except Exception:
                    pass


ws_manager = ConnectionManager()
