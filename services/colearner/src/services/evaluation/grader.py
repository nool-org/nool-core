"""Strict Bloom grader: gpt-5-mini via Structured Outputs (strict JSON schema).

The model grades each answer; every aggregate (mastery, per-Bloom means,
hint penalty) is computed here in code, never trusted to the model.
"""
import json
import logging
from statistics import mean
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from shared import llm_gateway

from src.config.settings import get_settings
from src.services.cost_service import MODEL_PRICING, calculate_turn_cost, record_turn_metrics
from src.services.session_store import SessionState

logger = logging.getLogger(__name__)

GRADER_MODEL = "gpt-5-mini"
PROMPT_VERSION = "strict-v3"
HINT_PENALTY = 10      # points per hint used
HINT_PENALTY_MAX = 20  # per answer
EASIER_MAX_SCORE = 70  # an answer to a question's easier version

_client: Optional[AsyncOpenAI] = None

_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "question_number": {"type": "integer"},
        "blooms_attribute": {
            "type": "string",
            "enum": ["Remembering", "Understanding", "Applying", "Analyzing", "Evaluating", "Creating"],
        },
        "question_asked": {"type": "string"},
        "student_response": {"type": "string"},
        "probing_or_hints_given": {"type": ["string", "null"]},
        "points_covered": {"type": "array", "items": {"type": "string"}},
        "misconception": {"type": ["string", "null"]},
        "score_percent": {"type": "integer"},
        "is_substantively_correct": {"type": "boolean"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "hints_used": {"type": "integer"},
    },
    "required": [
        "question_number", "blooms_attribute", "question_asked", "student_response", "probing_or_hints_given",
        "points_covered", "misconception", "score_percent", "is_substantively_correct", "reason", "confidence",
        "hints_used",
    ],
}

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "bloom_grading",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "overall_feedback": {"type": "string"},
                "answers": {"type": "array", "items": _ANSWER_SCHEMA},
            },
            "required": ["overall_feedback", "answers"],
        },
    },
}

SYSTEM_PROMPT = """You are an honest, strict evaluator of a school student's oral answers, mapping each to Bloom's Taxonomy. You are not a cheerleader.

For every substantive question the Co-Learner asked, and the student's answer to it, return one entry:
- question_number: 1, 2, 3... in the order asked.
- blooms_attribute: the Bloom's level the question targets.
- question_asked: the question itself (without the Co-Learner's acknowledgement of the previous answer).
- student_response: the student's answer, close to verbatim.
- probing_or_hints_given: any hint or probe the Co-Learner gave on this question, else null.
- points_covered: the correct key points the answer actually states (empty if none).
- misconception: the specific wrong idea the answer contains, else null.
- score_percent: 0-100 for how well the answer shows correct understanding at the target level.
- is_substantively_correct: true only if the core idea of the answer is scientifically/factually correct.
- reason: one line justifying the score.
- confidence: 0-1, how sure you are of this verdict.
- hints_used: number of hints the student was given on this question (0 if none).

Rules:
- Judge correctness against standard textbook knowledge for the stated grade and subject. Check every claim.
- A confident but wrong answer scores low (under 40) and is NOT substantively correct. Fluency, length and keywords are not correctness.
- A vague, evasive, or "I don't know" answer scores low and is NOT substantively correct.
- Any answer whose central claim is wrong is NOT substantively correct, even if parts of it are right.
- A claim that breaks a basic principle (e.g. energy being created or used up, charge being consumed) is a misconception, even if worded casually.
- Judge directions, conventions and definitions by the textbook for the stated grade: loose wording that makes the stated fact false (e.g. calling electron flow "electricity" or "current" and giving the wrong direction for it) is wrong.
- A correct but shallow answer is substantively correct with a moderate score; a correct, well-reasoned answer scores high.
- These are spoken answers: judge meaning, not wording, grammar or exact terms - a correct idea in everyday words counts.
- Students may mix English with Tamil, Hindi, Telugu, Kannada, Malayalam or another language (Tanglish, Hinglish): judge the meaning regardless of the language mix, never mark it down.
- Every student turn tagged with the same [Qn] is one answer: a reply to the Co-Learner's follow-up ("Can you tell me why?", "How would you explain that to a friend?") or after a hint is judged together with the first reply. Ignore turns marked (repeat request) or (noise).
- Partial credit, against the question's expected key points (or the textbook answer if no rubric): all key points stated correctly = 85-100; about half of them correct = 50-60 (partial, is_substantively_correct true only if the core idea is right); a little relevant but mostly missing = 20-40; none, off-topic or "I don't know" = 0-15. A confidently wrong central claim stays under 40 whatever else is covered.
- Do NOT include the opening readiness exchange (e.g. "Are you ready to begin?") or closing small talk.
- If a rubric is given, use its expected key points and known misconceptions for that question.
- A rubric entry with version "easier" means the student was asked a simpler version of the question (its text is in the rubric): grade against that version, and score it at most 70.

overall_feedback: 1-2 sentences on the student's engagement and conceptual understanding."""


def _get_client() -> Optional[AsyncOpenAI]:
    global _client
    key = get_settings().openai_api_key
    if _client is None and key:
        _client = AsyncOpenAI(**llm_gateway.client_kwargs(key), timeout=90.0, max_retries=2)
    return _client


def _user_prompt(transcript: str, grade: str, subject: str, chapter: str, rubric: Optional[List[Dict[str, Any]]]) -> str:
    rubric_part = (
        f"\nRUBRIC (expected key points and known misconceptions per question):\n{json.dumps(rubric, ensure_ascii=False)}\n"
        if rubric else ""
    )
    return (
        f"Grade: {grade}\nSubject: {subject}\nChapter: {chapter}\n{rubric_part}\n"
        f'TRANSCRIPT:\n"""\n{transcript}\n"""'
    )


def _clamp_score(score: Any) -> int:
    return max(0, min(100, int(score or 0)))


def penalized_score(score: Any, hints: Any) -> int:
    score = _clamp_score(score)
    return max(0, score - min(HINT_PENALTY * max(0, int(hints or 0)), HINT_PENALTY_MAX))


def build_report(graded: Dict[str, Any], grade: str, subject: str, chapter: str, num_questions: int,
                 rubric: Optional[List[Dict[str, Any]]] = None,
                 planned: Optional[List[tuple]] = None) -> Dict[str, Any]:
    """Turns the model's per-answer grades into the report core consumes, aggregates computed here.
    `planned` [(bloom level, question text)] in order: questions after the last one answered weren't
    reached in time and score 0 - a slow student is behind, and the mastery says so."""
    easier = {r.get("question_number") for r in rubric or [] if r.get("version") == "easier"}
    qa = []
    for a in graded.get("answers") or []:
        raw = _clamp_score(a["score_percent"])
        capped = min(raw, EASIER_MAX_SCORE) if a.get("question_number") in easier else raw
        qa.append({
            **a,
            "raw_score_percent": raw,
            "score_percent": penalized_score(capped, a.get("hints_used")),
            "confidence": max(0.0, min(1.0, float(a.get("confidence") or 0))),
        })
    last = max((a["question_number"] for a in qa if isinstance(a.get("question_number"), int)), default=0)
    for n, (level, text) in enumerate(planned or [], start=1):
        if n > last:
            qa.append({"question_number": n, "blooms_attribute": level, "question_asked": text, "student_response": "",
                       "probing_or_hints_given": None, "points_covered": [], "misconception": None,
                       "score_percent": 0, "raw_score_percent": 0, "is_substantively_correct": False,
                       "reason": "Not reached - the time ran out before this question.", "confidence": 1.0,
                       "hints_used": 0, "not_reached": True})
    by_level: Dict[str, List[int]] = {}
    for a in qa:
        by_level.setdefault(a["blooms_attribute"], []).append(a["score_percent"])
    return {
        "session_summary": {
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "total_questions_asked": num_questions,
            "overall_feedback": graded.get("overall_feedback") or "",
            "overall_mastery_percent": round(mean(a["score_percent"] for a in qa)) if qa else 0,
            "per_bloom": {level: round(mean(s)) for level, s in by_level.items()},
        },
        "qa_transcripts": qa,
        "model": GRADER_MODEL,
        "prompt_version": PROMPT_VERSION,
    }


def _cost_metrics(usage: Any) -> Dict[str, Any]:
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    metrics = calculate_turn_cost(GRADER_MODEL, usage.prompt_tokens, usage.completion_tokens)
    # calculate_turn_cost bills all input at the uncached rate - credit the cached part back
    pricing = MODEL_PRICING[GRADER_MODEL]
    saving = cached / 1_000_000 * (pricing["input_per_million"] - pricing["cached_input_per_million"])
    metrics["input_cost_usd"] = round(metrics["input_cost_usd"] - saving, 7)
    metrics["turn_cost_usd"] = round(metrics["turn_cost_usd"] - saving, 6)
    metrics["cached_input_tokens"] = cached
    return metrics


def _planned(config) -> List[tuple]:
    """The test's questions in order as (bloom level, text) - prepared ones, else the levels it improvises on."""
    if config.questions:
        return [(q.bloom_level, q.spoken_text) for q in config.questions]
    levels = list(config.blooms_attributes or [])
    return [(levels[i % len(levels)] if levels else "Understanding", "") for i in range(config.num_questions)]


def _record_cost(session: SessionState, usage: Any) -> None:
    if usage:
        record_turn_metrics(session, _cost_metrics(usage))


async def grade_transcript(
    session: SessionState,
    transcript: str,
    rubric: Optional[List[Dict[str, Any]]] = None,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Grades a full session transcript. Records the call's real cost on `session`.

    rubric: optional [{question_number, key_points: [...], misconceptions: [...], version?}] -
    a "version": "easier" answer is capped at EASIER_MAX_SCORE.
    Never fakes success: on failure returns qa_transcripts [], `grading_error`, and
    session_summary.overall_mastery_percent = None (consumers must handle null).
    """
    config = session.config
    report_args = (config.grade, config.subject, config.chapter_name, config.num_questions)
    try:
        client = client or _get_client()
        if client is None:
            raise RuntimeError("openai_api_key is not configured")
        resp = await client.chat.completions.create(
            model=GRADER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(transcript, *report_args[:3], rubric)},
            ],
            response_format=RESPONSE_FORMAT,
            **llm_gateway.call_kwargs("chat"),
        )
        _record_cost(session, getattr(resp, "usage", None))
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise RuntimeError(f"grader refused: {msg.refusal}")
        return build_report(json.loads(msg.content), *report_args, rubric=rubric, planned=_planned(config))
    except Exception as ex:
        logger.exception("Bloom grading failed for session %s", session.session_id)
        report = build_report({}, *report_args)
        report["session_summary"]["overall_mastery_percent"] = None
        report["grading_error"] = f"{type(ex).__name__}: {ex}"
        return report


# ---- one short practice answer (daily practice, core's practice_voice.py) ----
ANSWER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer_grading",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "score_percent": {"type": "integer"},
                "good": {"type": "string"},
                "add": {"type": "string"},
            },
            "required": ["score_percent", "good", "add"],
        },
    },
}

ANSWER_SYSTEM_PROMPT = """You are an honest, strict evaluator of ONE short spoken answer a school student gave to a practice question. You are not a cheerleader.

Return:
- score_percent: 0-100 for how well the answer shows correct understanding.
- good: one short sentence (under 15 words) on what the answer got right, addressed to the student ("You named both gases correctly."). Empty string if nothing was right.
- add: one short sentence (under 15 words): the single most important thing to add or fix, as a pointer, e.g. "Add why light bends: its speed changes." Never recite the full answer. Empty string only if the answer is complete.

Rules:
- Judge correctness against standard textbook knowledge for the subject. Check every claim.
- A confident but wrong answer scores low (under 40). Fluency, length and keywords are not correctness.
- A vague, evasive, or "I don't know" answer scores 0-15.
- A claim that breaks a basic principle (e.g. energy being created or used up) is a misconception, even if worded casually - point "add" at fixing it.
- This is a spoken answer: judge meaning, not wording, grammar or exact terms - a correct idea in everyday words counts.
- Students may mix English with Tamil, Hindi, Telugu, Kannada, Malayalam or another language (Tanglish, Hinglish): judge the meaning regardless of the language mix, never mark it down.
- Partial credit, against the expected key points (or the textbook answer if none are given): all key points stated correctly = 85-100; about half of them correct = 50-60; a little relevant but mostly missing = 20-40; none, off-topic or "I don't know" = 0-15. A confidently wrong central claim stays under 40 whatever else is covered.
- If known misconceptions are given and the answer states one, score it under 40."""

EMPTY_ANSWER = {"score": 0, "good": "", "add": "Say your answer out loud - even a short one counts."}


async def grade_answer(
    question: str,
    expected_points: List[str],
    misconceptions: List[str],
    transcript: str,
    subject: Optional[str] = None,
    client: Optional[Any] = None,
    caller: Optional[Dict[str, str]] = None,
    metrics: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """{score 0-100, good, add} for one spoken answer. Empty transcript -> EMPTY_ANSWER,
    no model call. Raises on any failure (no key, API error, refusal) - never fakes a grade.
    `caller` (Core's feature/student ids) tags the grade_answer_cost log line; the call's
    cost metrics are appended to `metrics` (before any refusal/parse failure - it was billed)."""
    if not transcript.strip():
        return dict(EMPTY_ANSWER)
    client = client or _get_client()
    if client is None:
        raise RuntimeError("openai_api_key is not configured")
    prompt = json.dumps({"subject": subject, "question": question, "expected_key_points": expected_points,
                         "known_misconceptions": misconceptions}, ensure_ascii=False)
    resp = await client.chat.completions.create(
        model=GRADER_MODEL,
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": f'{prompt}\nSTUDENT ANSWER:\n"""\n{transcript}\n"""'},
        ],
        response_format=ANSWER_RESPONSE_FORMAT,
        **llm_gateway.call_kwargs("chat"),
    )
    if usage := getattr(resp, "usage", None):
        call_cost = _cost_metrics(usage)
        if metrics is not None:
            metrics.append(call_cost)
        logger.info("grade_answer_cost", extra={"cost": {**call_cost, **(caller or {})}})
    msg = resp.choices[0].message
    if getattr(msg, "refusal", None):
        raise RuntimeError(f"grader refused: {msg.refusal}")
    data = json.loads(msg.content)
    return {"score": _clamp_score(data["score_percent"]), "good": str(data.get("good") or "").strip(),
            "add": str(data.get("add") or "").strip()}
