"""WebSocket client for the colearner service (services/colearner) - the
real voice-conversation pipeline behind the AI Assessor's WS stream (see
src/api/routes/ai_assessor.py's stream_session). Mirrors kg_client.py's
role (the one module that talks to an external nool-core service) but over
a WebSocket instead of HTTP request/response, since a colearner session is
a live, stateful, bidirectional conversation, not one call-and-response.

Auth: colearner requires a real Firebase-verified caller on its own WS (see
services/colearner/src/api/deps.py) - Core forwards the exact ID token the
browser/app already sent it on Core's own stream, rather than inventing a
separate service-to-service credential. colearner sees this as "the
student's own session," which is what it is.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from shared.errors import AppError

from src.config.settings import get_settings

_CONNECT_TIMEOUT_SECONDS = 10.0
# A turn involves a real LLM (+ TTS) call - a few seconds normally, but
# bounded generously here so a wedged/half-closed connection (rather than
# a clean ConnectionClosed) can't hang the bridge indefinitely.
_TURN_TIMEOUT_SECONDS = 45.0


class ColearnerUnavailableError(AppError):
    code = "COLEARNER_UNAVAILABLE"
    status_code = 503


def _ws_url(pipeline: str, token: str) -> str:
    settings = get_settings()
    if not settings.colearner_service_url:
        raise ColearnerUnavailableError("COLEARNER_SERVICE_URL is not configured.")
    base = settings.colearner_service_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws_base}/api/v1/colearner/{pipeline}/stream?token={token}"


class ColearnerSession:
    """One live colearner conversation. Every send_*/close call
    corresponds to exactly one WS frame each way - see
    services/colearner/README.md's WS protocol description.
    """

    def __init__(self, ws: websockets.WebSocketClientProtocol) -> None:
        self._ws = ws

    @classmethod
    async def connect(cls, *, pipeline: str, token: str) -> "ColearnerSession":
        url = _ws_url(pipeline, token)
        try:
            ws = await asyncio.wait_for(websockets.connect(url), timeout=_CONNECT_TIMEOUT_SECONDS)
        except Exception as exc:
            raise ColearnerUnavailableError("Could not reach the colearner service.") from exc
        return cls(ws)

    async def send_config(self, config: dict) -> dict:
        """First frame after connecting - colearner replies with the Turn 0
        greeting (see services/colearner/src/api/routes/chained.py's
        stream_chained_session).
        """
        return await self._send_and_receive({"config": config})

    async def send_turn(
        self, *, text: str | None = None, audio_base64: str | None = None, time_limit_reached: bool = False
    ) -> dict:
        frame: dict = {"time_limit_reached": time_limit_reached}
        if audio_base64:
            frame["user_audio_base64"] = audio_base64
        elif text is not None:
            frame["user_text"] = text
        return await self._send_and_receive(frame)

    async def send_hint_request(self) -> dict:
        """A guiding-nudge request, not an answer - see
        chained_service.py's/multimodal_service.py's `is_hint_request`
        branch. Never counts as a turn advance, never reveals or confirms
        correctness; the reply comes back with `is_hint_reply: true`.
        """
        return await self._send_and_receive({"time_limit_reached": False, "is_hint_request": True})

    async def _send_and_receive(self, frame: dict) -> dict:
        try:
            await asyncio.wait_for(self._ws.send(json.dumps(frame)), timeout=_TURN_TIMEOUT_SECONDS)
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_TURN_TIMEOUT_SECONDS)
        except ConnectionClosed as exc:
            raise ColearnerUnavailableError("The colearner session closed unexpectedly.") from exc
        except asyncio.TimeoutError as exc:
            raise ColearnerUnavailableError("The colearner service did not respond in time.") from exc
        return json.loads(raw)

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


# ---- Realtime (live WebRTC voice) - plain HTTP, not the per-turn WS above ----

def _http_base() -> str:
    settings = get_settings()
    if not settings.colearner_service_url:
        raise ColearnerUnavailableError("COLEARNER_SERVICE_URL is not configured.")
    return settings.colearner_service_url.rstrip("/")


async def _realtime_post(path: str, body: dict, *, token: str, timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(base_url=_http_base(), timeout=timeout) as client:
            response = await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ColearnerUnavailableError("Could not reach the colearner service.") from exc
    if response.status_code == 404:
        raise ColearnerUnavailableError("This voice session has expired - please start it again.")
    if response.status_code >= 300:
        raise ColearnerUnavailableError("The live voice service couldn't handle the request.")
    return response.json()


async def create_realtime_call(*, sdp: str, config: dict, owner_ref: str, token: str) -> dict:
    """{"sdp": <answer>, "call_id": ...} - see colearner's routes/realtime.py."""
    return await _realtime_post(
        "/api/v1/colearner/realtime/calls",
        {"sdp": sdp, "config": config, "owner_ref": owner_ref}, token=token, timeout=30.0,
    )


async def finish_realtime_call(*, call_id: str, owner_ref: str, token: str) -> dict:
    """{"report", "turns", "answered", "model_completed"} - scoring runs an
    LLM call, hence the longer timeout."""
    return await _realtime_post(
        f"/api/v1/colearner/realtime/calls/{call_id}/finish",
        {"owner_ref": owner_ref}, token=token, timeout=90.0,
    )
