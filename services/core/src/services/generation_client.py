"""HTTP client for the generation service (services/generation) - LLM
question writing for the homework slots the question bank couldn't fill,
and rewording a voice test's picked questions for speech. Same shape as
kg_client.py."""

from __future__ import annotations

import logging

import httpx

from shared.errors import AppError
from src.config.settings import get_settings
from src.services import ai_context

logger = logging.getLogger(__name__)

# The service generates, verifies and may regenerate once - ~1 min typical.
GAP_FILL_TIMEOUT_SECONDS = 180.0
VOICE_TEST_PREP_TIMEOUT_SECONDS = 120.0
ASSISTANT_TIMEOUT_SECONDS = 45.0
TRANSCRIBE_TIMEOUT_SECONDS = 60.0


class GenerationServiceUnavailableError(AppError):
    code = "GENERATION_SERVICE_UNAVAILABLE"
    status_code = 503


def is_configured() -> bool:
    return bool(get_settings().generation_service_url)


async def gap_fill(payload: dict) -> dict:
    """POST /v1/homework/gap-fill - see services/generation/src/services/gap_fill.py
    for the request/response shape."""
    return await _post("/v1/homework/gap-fill", payload, GAP_FILL_TIMEOUT_SECONDS, "homework")


async def generate_mcq(payload: dict) -> dict:
    """Gap-fill with mcq slots ({bloom_level, difficulty, question_type: "mcq",
    concept, misconceptions}) - generated and verified like homework questions,
    each with exactly 4 options and the answer one of them."""
    return await _post("/v1/homework/gap-fill", payload, GAP_FILL_TIMEOUT_SECONDS, "mcq_check")


async def explain_mcq(payload: dict) -> dict:
    """POST /v1/mcq/explain - {items: [{question_number, text, options, correct_index,
    concept}]} -> {results: [{question_number, explanation, hint}]} (null when unusable)."""
    return await _post("/v1/mcq/explain", payload, GAP_FILL_TIMEOUT_SECONDS, "mcq_explain")


async def prep_voice_test(payload: dict) -> dict:
    """POST /v1/voice-test/prep - {grade, subject, chapter, board, levels,
    candidates} -> {questions: [...]}, one reworded question per level."""
    return await _post("/v1/voice-test/prep", payload, VOICE_TEST_PREP_TIMEOUT_SECONDS, "voice_prep")


async def transcribe(audio_base64: str, mime_type: str, prompt: str, language: str = "en") -> str:
    """POST /v1/transcribe (gpt-4o-mini-transcribe) - the recognised text, stripped.
    language "auto" for students' answers, which may mix English with Indian languages."""
    body = await _post("/v1/transcribe", {"audio_base64": audio_base64, "mime_type": mime_type, "prompt": prompt,
                                          "language": language}, TRANSCRIBE_TIMEOUT_SECONDS,
                       "practice_transcribe")
    return str(body.get("text") or "").strip()


async def assistant_reply(facts: dict, history: list[dict], message: str) -> str:
    """Teacher assistant chat turn, grounded in teacher_context facts."""
    if not is_configured():
        raise GenerationServiceUnavailableError("generation service is not configured")
    body = await _post("/v1/teacher-assistant/reply", {"facts": facts, "history": history, "message": message},
                       ASSISTANT_TIMEOUT_SECONDS, "assistant")
    return body["reply"]


async def teacher_summary(facts: dict) -> list[str]:
    if not is_configured():
        raise GenerationServiceUnavailableError("generation service is not configured")
    body = await _post("/v1/teacher-assistant/summary", {"facts": facts}, ASSISTANT_TIMEOUT_SECONDS, "summary")
    return list(body.get("bullets") or [])


async def _post(path: str, payload: dict, timeout: float, feature: str) -> dict:
    """`feature` labels the call in generation's cost log unless ai_context.feature() overrides it."""
    base_url = get_settings().generation_service_url
    if not base_url:
        raise GenerationServiceUnavailableError("GENERATION_SERVICE_URL is not configured.")
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
            response = await client.post(path, json=payload, headers=ai_context.headers(feature))
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.warning("generation_service_upstream_error", extra={"path": path, "reason": str(exc)})
        raise GenerationServiceUnavailableError("Could not reach the generation service.") from exc
