"""HTTP client for the colearner service's realtime (live WebRTC voice)
calls (services/colearner/src/api/routes/realtime.py). Mirrors
kg_client.py's role: the one module that talks to that nool-core service.

Auth: colearner requires a real Firebase-verified caller (see
services/colearner/src/api/deps.py) - Core forwards the exact ID token the
app already sent it, rather than inventing a separate service-to-service
credential.
"""

from __future__ import annotations

import httpx

from shared.errors import AppError

from src.config.settings import get_settings


class ColearnerUnavailableError(AppError):
    code = "COLEARNER_UNAVAILABLE"
    status_code = 503


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


async def regrade_transcript(*, config: dict, transcript: list, answers: list, token: str) -> dict:
    """The C2 report for a stored session (colearner's routes/regrade.py) - may
    carry `grading_error`. An LLM call, hence the longer timeout."""
    return await _realtime_post(
        "/api/v1/colearner/regrade",
        {"config": config, "transcript": transcript, "answers": answers}, token=token, timeout=90.0,
    )


async def grade_answer(*, question: str, expected_points: list[str], misconceptions: list[str], transcript: str,
                       subject: str | None, token: str) -> dict:
    """{score 0-100, good, add} for one short spoken practice answer
    (colearner's routes/regrade.py grade-answer) - one cheap LLM call."""
    return await _realtime_post(
        "/api/v1/colearner/grade-answer",
        {"question": question, "expected_points": expected_points, "misconceptions": misconceptions,
         "transcript": transcript, "subject": subject}, token=token, timeout=30.0,
    )
