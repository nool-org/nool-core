"""Answer key + board-examiner marking scheme for a teacher's question paper.

    generate (GENERATION_MODEL, one call per item, parallel)
      -> code checks (marks sum == question marks; MCQ answer is one of the options)
      -> verify (VERIFY_MODEL, one batch call: answer correct? step marks sensible?)
      -> failed items regenerate once with the reason as feedback
      -> items still failing come back in `failed_ids`, never as an unverified key.

When core sends a `reference_answer` (the question came from the bank, which
stores a vetted answer/solution) the model must keep that answer and only lay
out the marking points.
"""

import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from src.services.gap_fill import BloomLevel
from src.services.llm import StructuredLLM

logger = logging.getLogger(__name__)

MAX_TEXT = 6000


# ---- request / result ----------------------------------------------------
class KeyItem(BaseModel):
    id: str = Field(max_length=100)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    question_type: Literal["MCQ", "SHORT_ANSWER", "LONG_ANSWER", "TRUE_FALSE"] | None = None
    marks: float = Field(gt=0, le=20)
    options: list[str] | None = Field(default=None, max_length=6)
    bloom_level: BloomLevel | None = None
    reference_answer: str | None = Field(default=None, max_length=MAX_TEXT)
    reference_solution: str | None = Field(default=None, max_length=MAX_TEXT)


class AnswerKeyRequest(BaseModel):
    board: str = Field(default="CBSE", max_length=100)
    grade: int = Field(ge=1, le=12)
    subject: str = Field(max_length=200)
    items: list[KeyItem] = Field(min_length=1, max_length=60)


class MarkingPoint(BaseModel):
    point: str = Field(description="one value point / step the examiner looks for")
    marks: float = Field(description="marks for this point; halves (0.5) allowed")


class KeyedItem(BaseModel):
    id: str
    answer: str
    marking_points: list[MarkingPoint]


class AnswerKeyResult(BaseModel):
    items: list[KeyedItem]
    failed_ids: list[str]


# ---- LLM output shapes ---------------------------------------------------
class _Key(BaseModel):
    answer: str = Field(description="the model answer; for MCQ the correct option text, then a one-line reason")
    marking_points: list[MarkingPoint]


class _Verdict(BaseModel):
    id: str
    ok: bool
    problem: str = Field(description="empty when ok; otherwise what is wrong, specifically")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


GENERATE_SYSTEM = """You are a {board} Class {grade} {subject} board examiner writing the answer key and
marking scheme for one question of a school exam paper.
- answer: the complete model answer a full-marks student would write.
- marking_points: step marking as {board} examiners do - value points, the formula, substitution,
  the final result with its unit - each with the marks it earns. The marks must add up EXACTLY to the
  question's `marks`. Halves (0.5) are allowed; no point is worth 0.
- MCQ / true-false: `answer` is the correct option exactly as written, then " - " and a one-line reason;
  a single marking point for the correct choice worth all the marks.
- If `reference_answer` is given it is the vetted answer: keep it (you may tidy wording) and only lay out
  the marking points from it and `reference_solution`.
If `feedback` is given, your previous attempt was rejected for that reason - fix it."""

VERIFY_SYSTEM = """You are a strict {board} Class {grade} {subject} chief examiner checking answer keys.
For each item, solve the question yourself, then ok=true only if ALL hold:
1. The answer is correct and complete for the question (for MCQ: the right option).
2. The marking points are real, distinct steps an examiner can award, fair for the marks.
3. The marking points match the answer (no point the answer doesn't contain).
If not ok, say precisely what is wrong in `problem`."""


# ---- code checks ---------------------------------------------------------
def problem(item: KeyItem, key: _Key) -> str | None:
    """Everything checkable without a model; None when clean."""
    if not key.answer.strip() or not key.marking_points:
        return "answer and marking_points are required"
    if any(p.marks <= 0 or not p.point.strip() for p in key.marking_points):
        return "every marking point needs text and positive marks"
    total = sum(p.marks for p in key.marking_points)
    if abs(total - item.marks) > 1e-6:
        return f"marking points add up to {total:g}, must be exactly {item.marks:g}"
    if item.options and not any(key.answer.strip().lower().startswith(o.strip().lower()) for o in item.options):
        return "for MCQ, start the answer with the correct option exactly as written"
    return None


# ---- run -----------------------------------------------------------------
async def answer_key(llm: StructuredLLM, request: AnswerKeyRequest, *, generation_model: str, verify_model: str,
                     max_attempts: int = 2) -> AnswerKeyResult:
    system = GENERATE_SYSTEM.format(board=request.board, grade=request.grade, subject=request.subject)
    verify_system = VERIFY_SYSTEM.format(board=request.board, grade=request.grade, subject=request.subject)
    by_id = {i.id: i for i in request.items}
    accepted: dict[str, _Key] = {}
    pending, feedback = list(by_id), {}

    async def one(item_id: str) -> _Key | None:
        payload = {**by_id[item_id].model_dump(exclude_none=True, exclude={"id"}),
                   **({"feedback": feedback[item_id]} if item_id in feedback else {})}
        try:
            return await llm(model=generation_model, effort="low", system=system,
                             user=json.dumps(payload, ensure_ascii=False), output_type=_Key)
        except Exception as exc:  # one item's failure mustn't sink the others
            logger.warning("answer_key_generate_failed", extra={"item": item_id, "error": type(exc).__name__})
            return None

    for _ in range(max_attempts):
        drafts = dict(zip(pending, await asyncio.gather(*(one(i) for i in pending))))
        clean = {}
        for item_id, key in drafts.items():
            reason = "nothing was produced" if key is None else problem(by_id[item_id], key)
            if reason:
                feedback[item_id] = reason
            else:
                clean[item_id] = key
        if clean:
            payload = {"items": [{"id": i, **by_id[i].model_dump(exclude_none=True, exclude={"id"}),
                                  **k.model_dump()} for i, k in clean.items()]}
            try:
                verdicts = await llm(model=verify_model, effort="low", system=verify_system,
                                     user=json.dumps(payload, ensure_ascii=False), output_type=_Verdicts)
            except Exception as exc:  # unverified keys count as failed
                logger.warning("answer_key_verify_failed", extra={"items": len(clean), "error": type(exc).__name__})
                verdicts = _Verdicts(verdicts=[])
            verdict = {v.id: v for v in verdicts.verdicts}
            for item_id, key in clean.items():
                if item_id in verdict and verdict[item_id].ok:
                    accepted[item_id] = key
                else:
                    feedback[item_id] = verdict[item_id].problem if item_id in verdict else "not verified"
        pending = [i for i in pending if i not in accepted]
        if not pending:
            break

    return AnswerKeyResult(
        items=[KeyedItem(id=i, answer=accepted[i].answer.strip(), marking_points=accepted[i].marking_points)
               for i in by_id if i in accepted],
        failed_ids=pending,
    )
