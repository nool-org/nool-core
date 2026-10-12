"""Generation-service calls for answer keys and Set B papers - same transport
and errors as generation_client.py (GenerationServiceUnavailableError -> 503)."""

from src.services.generation_client import (  # noqa: F401 - re-exported for callers
    GAP_FILL_TIMEOUT_SECONDS,
    GenerationServiceUnavailableError,
    _post,
    is_configured,
)

ANSWER_KEY_TIMEOUT_SECONDS = 180.0


async def answer_key(payload: dict) -> dict:
    """POST /v1/paper/answer-key - {board, grade, subject, items: [{id, text, question_type, marks,
    options, bloom_level, reference_answer, reference_solution}]} -> {items: [{id, answer,
    marking_points: [{point, marks}]}], failed_ids}."""
    return await _post("/v1/paper/answer-key", payload, ANSWER_KEY_TIMEOUT_SECONDS, "paper")


async def gap_fill(payload: dict) -> dict:
    """POST /v1/homework/gap-fill - the generic "write verified questions for these
    (bloom, difficulty) slots from this textbook text" path, reused for Set B."""
    return await _post("/v1/homework/gap-fill", payload, GAP_FILL_TIMEOUT_SECONDS, "paper")
