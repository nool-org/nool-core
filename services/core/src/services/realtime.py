"""In-process WebSocket fan-out - see api/routes/realtime.py for the two
channels built on this: a broadcast channel any teacher/student client
subscribes to (curriculum_updates) and one progress channel per admin sync
job (sync_progress_channel).

ponytail: single-process, in-memory connection sets - a second Core replica
never sees connections made to another instance. Fine at today's single-
instance scale; upgrade to Redis pub/sub if Core ever runs >1 replica.
"""

from __future__ import annotations

from fastapi import WebSocket


class Channel:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for connection in self._connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self._connections.discard(connection)


curriculum_updates = Channel()

_sync_progress_channels: dict[str, Channel] = {}


def sync_progress_channel(job_id: str) -> Channel:
    return _sync_progress_channels.setdefault(job_id, Channel())


def drop_sync_progress_channel(job_id: str) -> None:
    _sync_progress_channels.pop(job_id, None)
