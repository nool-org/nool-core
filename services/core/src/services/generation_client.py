"""HTTP client for the generation service (services/generation) - LLM
question writing for the homework slots the question bank couldn't fill,
and rewording a voice test's picked questions for speech. Same shape as
kg_client.py."""

from __future__ import annotations

import logging

import httpx

from shared.errors import AppError
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

# The service generates, verifies and may regenerate once - ~1 min typical.
GAP_FILL_TIMEOUT_SECONDS = 180.0
VOICE_TEST_PREP_TIMEOUT_SECONDS = 120.0


class GenerationServiceUnavailableError(AppError):
    code = "GENERATION_SERVICE_UNAVAILABLE"
    status_code = 503


def is_configured() -> bool:
    return bool(get_settings().generation_service_url)


async def gap_fill(payload: dict) -> dict:
    """POST /v1/homework/gap-fill - see services/generation/src/services/gap_fill.py
    for the request/response shape."""
    return await _post("/v1/homework/gap-fill", payload, GAP_FILL_TIMEOUT_SECONDS)


async def prep_voice_test(payload: dict) -> dict:
    """POST /v1/voice-test/prep - {grade, subject, chapter, board, levels,
    candidates} -> {questions: [...]}, one reworded question per level."""
    return await _post("/v1/voice-test/prep", payload, VOICE_TEST_PREP_TIMEOUT_SECONDS)


async def _post(path: str, payload: dict, timeout: float) -> dict:
    base_url = get_settings().generation_service_url
    if not base_url:
        raise GenerationServiceUnavailableError("GENERATION_SERVICE_URL is not configured.")
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.warning("generation_service_upstream_error", extra={"path": path, "reason": str(exc)})
        raise GenerationServiceUnavailableError("Could not reach the generation service.") from exc
