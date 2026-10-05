"""HTTP client for colearner's stateless homework-insight generation - see
services/colearner/src/api/routes/homework_insight.py. Mirrors
kg_client.py's REST request/response pattern (a one-shot call, no session,
colearner doesn't need to know which student asked: everything it needs
is in the request body) rather than
colearner_client.py's live WebSocket session, which is specific to the AI
Assessor's turn-taking conversation. Colearner requires a signed-in caller,
so core forwards the requesting student's own ID token, the same way
colearner_client.py does for live sessions.
"""

from __future__ import annotations

import logging

import httpx

from shared.errors import AppError

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0


class ColearnerInsightUnavailableError(AppError):
    code = "COLEARNER_INSIGHT_UNAVAILABLE"
    status_code = 503


def _base_url() -> str:
    settings = get_settings()
    if not settings.colearner_service_url:
        raise ColearnerInsightUnavailableError("COLEARNER_SERVICE_URL is not configured.")
    return settings.colearner_service_url.rstrip("/")


async def generate_homework_insight(
    *,
    topic: str,
    grade: str,
    subject: str,
    mastery_percent: int,
    weak_bloom_level: str,
    textbook_context: str | None,
    token: str | None = None,
) -> dict:
    """Returns {what_needs_understanding, references, key_idea_title,
    key_idea_body, connection_prompt} - see
    HomeworkInsightResponse in colearner's schemas.py for the exact shape.
    """
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "/api/v1/homework-insight",
                json={
                    "topic": topic,
                    "grade": grade,
                    "subject": subject,
                    "mastery_percent": mastery_percent,
                    "weak_bloom_level": weak_bloom_level,
                    "textbook_context": textbook_context,
                },
                headers={"Authorization": f"Bearer {token}"} if token else None,
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.warning("colearner_insight_upstream_error", extra={"reason": str(exc)})
        raise ColearnerInsightUnavailableError("Could not reach the insight service.") from exc
