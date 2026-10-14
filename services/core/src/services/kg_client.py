"""HTTP client for the kg service (services/kg) - the only thing in Core
that talks to the knowledge graph, and only ever through this module.
Mirrors shared/auth/providers/firebase.py's use of httpx for an external
call (the one existing precedent in this repo), but async - Core's route
handlers are all async, unlike Auth's sync token-verification path.
"""

from __future__ import annotations

import logging

import time

import httpx

from shared.errors import AppError

from src.config.settings import get_settings
from src.services import ai_context, ai_usage

logger = logging.getLogger(__name__)


class KgServiceUnavailableError(AppError):
    code = "KG_SERVICE_UNAVAILABLE"
    status_code = 503


def _base_url() -> str:
    settings = get_settings()
    if not settings.kg_service_url:
        raise KgServiceUnavailableError("KG_SERVICE_URL is not configured.")
    return settings.kg_service_url.rstrip("/")


# kg's model calls, by path - each one's cost is logged (ai_usage_events) like generation's
AI_FEATURES = {
    "/question-papers/generate": "paper_kg",
    "/question-papers/replacement-candidates": "paper_replace",
    "/voice-tests/generate": "voice_context",
}


async def _post(path: str, json: dict, *, timeout: float) -> dict:
    feature = AI_FEATURES.get(path)
    started = time.perf_counter()
    response = None
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=timeout) as client:
            response = await client.post(path, json=json, headers=ai_context.headers(feature) if feature else None)
            if feature:
                await ai_usage.record("kg", path, feature, started=started, response=response)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        if feature and response is None:  # never reached kg - a failed request, still on the books
            await ai_usage.record("kg", path, feature, started=started, error=exc)
        logger.warning("kg_service_upstream_error", extra={"path": path, "reason": str(exc)})
        raise KgServiceUnavailableError("Could not reach the knowledge-graph service. Try again.") from exc


async def _get(path: str, params: dict) -> dict | None:
    """Returns None on a 404 - a legitimate "nothing there" response from
    the kg service, not a sign it's unreachable. Callers that can get one
    (get_curriculum_tree) need to handle None explicitly; every other 4xx/5xx
    still means the service itself is the problem.
    """
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=30.0) as client:
            response = await client.get(path, params=params)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.warning("kg_service_upstream_error", extra={"path": path, "reason": str(exc)})
        raise KgServiceUnavailableError("Could not reach the knowledge-graph service. Try again.") from exc


async def get_curriculum_tree(*, board: str = "CBSE", grade: int = 10, subject: str = "Science") -> dict | None:
    """Used by POST /admin/curriculum/sync-from-kg and the per-Subject sync
    (admin_catalog.py). None means nool-kg has nothing for this
    board/grade/subject combination - not an error.
    """
    return await _get("/curriculum/tree", params={"board": board, "grade": grade, "subject": subject})


async def list_curriculum_subjects() -> list[dict]:
    """Real, populated subject/grade combinations nool-kg currently has -
    used by GET /admin/kg-subjects (admin_catalog.py) to let a Super Admin
    pick an exact KG subject instead of typing a name that has to match
    nool-kg's own naming exactly (see nool_kg_client.list_subjects's
    docstring on why a wrong name is worse than just "not found").
    """
    result = await _get("/curriculum/subjects", params={})
    return result or []


async def generate_paper_questions(
    *,
    subject: str,
    board: str,
    grade: int,
    chapters: list[dict],
    topic_names: list[str],
    bloom_distribution: dict[str, int],
    difficulty_distribution: dict[str, int],
    question_types: list[str],
    total_questions: int,
    reference_questions: list[dict] | None = None,
    slots: list[dict] | None = None,
) -> list[dict]:
    """Used by POST /question-papers/{id}/generate (question_paper.py).
    Grounded-question generation is not fast (one LLM call per figure-heavy
    chapter set) - a longer timeout than the other calls here.

    `subject`/`board`/`grade` scope which graph in nool-kg the kg service
    queries - the kg service defaults these to
    CBSE/Grade 10/Science when omitted, so a paper for any other subject
    would otherwise silently generate against the wrong (or a
    coincidentally-named) Curriculum tree with no visible error.
    """
    body = await _post(
        "/question-papers/generate",
        {
            "subject": subject,
            "board": board,
            "grade": grade,
            "chapters": chapters,
            "topic_names": topic_names,
            "bloom_distribution": bloom_distribution,
            "difficulty_distribution": difficulty_distribution,
            "question_types": question_types,
            "total_questions": total_questions,
            "reference_questions": reference_questions or [],
            "slots": slots or [],
        },
        timeout=120.0,
    )
    return body["questions"]


async def generate_assessor_content(
    *,
    subject: str,
    board: str,
    grade: int,
    chapters: list[dict],
    topic_names: list[str],
    bloom_levels: list[str],
    num_questions: int,
) -> dict:
    """Used by POST /tests (voice_test.py's create_test) - a grounded,
    spoken-friendly textbook context summary for the colearner's
    SessionConfig. Returns {"textbook_context": str, "reference_questions": []}:
    the test's questions now come from datasets via the generation service,
    so kg no longer generates any and reference_questions is always empty
    (kept so existing callers don't break). bloom_levels/num_questions are
    still sent for request compatibility. See generate_paper_questions'
    docstring for why subject/board/grade must be passed explicitly.
    """
    body = await _post(
        "/voice-tests/generate",
        {
            "subject": subject,
            "board": board,
            "grade": grade,
            "chapters": chapters,
            "topic_names": topic_names,
            "bloom_levels": bloom_levels,
            "num_questions": num_questions,
        },
        timeout=120.0,
    )
    return {"textbook_context": body.get("textbook_context") or "", "reference_questions": []}


async def generate_replacement_candidates(
    *,
    subject: str,
    board: str,
    grade: int,
    chapters: list[dict],
    topic_names: list[str],
    bloom_level: str,
    exclude_text: str,
    count: int,
) -> list[dict]:
    """Used by GET /question-papers/{id}/questions/{qid}/candidates (the
    Review screen's "Shuffle" picker). See generate_paper_questions'
    docstring for why subject/board/grade must be passed explicitly."""
    body = await _post(
        "/question-papers/replacement-candidates",
        {
            "subject": subject,
            "board": board,
            "grade": grade,
            "chapters": chapters,
            "topic_names": topic_names,
            "bloom_level": bloom_level,
            "exclude_text": exclude_text,
            "count": count,
        },
        timeout=60.0,
    )
    return body["questions"]
