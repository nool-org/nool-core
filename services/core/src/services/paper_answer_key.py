"""Answer key + marking scheme for a question paper (C13).

Keys are written by the generation service (POST /v1/paper/answer-key: gpt-5.1
writes board-examiner step marking, gpt-5-mini verifies). A question that came
from the bank sends the bank's vetted answer/solution as the reference, so the
model only lays out the marking points. A teacher-edited key (updated_by set)
is never regenerated unless the caller asks to overwrite.
"""

from sqlalchemy import select
from sqlalchemy.orm import undefer_group
from sqlalchemy.ext.asyncio import AsyncSession

from src.services import paper_actions
from src.services.paper_actions import COPILOT_COLUMNS

from shared.errors import NotFoundError, ValidationError
from src.api.schemas.paper_answer_key import (
    AnswerKeyItemOut, AnswerKeyOut, MarkingPointIO, UpdateAnswerKeyIn,
)
from src.domain.models import DatasetQuestion, QuestionPaper, QuestionPaperQuestion, Subject, User
from src.domain.models.paper_answer_key import QuestionPaperAnswerKey
from src.services import paper_answer_key_client as client
from src.services.question_selector import figure_caption

_TYPES = {"MCQ", "SHORT_ANSWER", "LONG_ANSWER", "TRUE_FALSE"}


def _v(x):
    """Enum -> its value; anything else as is."""
    return getattr(x, "value", x)


def marks_match(points: list[dict], marks: float) -> bool:
    return abs(sum(float(p["marks"]) for p in points) - marks) < 1e-6


async def _questions(session: AsyncSession, paper_id) -> list[QuestionPaperQuestion]:
    # through paper_actions so the deferred copilot columns (type, answer, ...) are loaded
    return await paper_actions.load_questions(session, paper_id)


async def _keys(session: AsyncSession, questions) -> dict:
    if not questions:
        return {}
    rows = (await session.execute(select(QuestionPaperAnswerKey).where(
        QuestionPaperAnswerKey.question_id.in_([q.id for q in questions])))).scalars().all()
    return {k.question_id: k for k in rows}


def _item(q: QuestionPaperQuestion, k: QuestionPaperAnswerKey) -> AnswerKeyItemOut:
    return AnswerKeyItemOut(question_id=str(q.id), answer=k.answer, total_marks=q.marks,
                            marking_points=[MarkingPointIO(**p) for p in k.marking_points])


async def get_answer_key(session: AsyncSession, paper: QuestionPaper) -> AnswerKeyOut:
    """Keys that exist, in paper order - a question without one is simply absent."""
    questions = await _questions(session, paper.id)
    keys = await _keys(session, questions)
    return AnswerKeyOut(items=[_item(q, keys[q.id]) for q in questions if q.id in keys])


async def generate_answer_key(session: AsyncSession, paper: QuestionPaper, *, regenerate: bool = False,
                              overwrite: bool = False) -> AnswerKeyOut:
    """Default: only questions with no key yet. `regenerate`: AI-written keys
    too. `overwrite`: teacher-edited keys too. Raises 503 when the generation
    service is unavailable - nothing is ever filled in without it."""
    questions = await _questions(session, paper.id)
    keys = await _keys(session, questions)
    targets = [q for q in questions if q.id not in keys or overwrite
               or (regenerate and keys[q.id].updated_by is None)]
    if not targets:
        return await get_answer_key(session, paper)
    if not client.is_configured():
        raise client.GenerationServiceUnavailableError("Answer keys can't be generated right now.")

    bank_ids = [getattr(q, "dataset_question_id", None) for q in targets]
    bank = {d.id: d for d in (await session.execute(select(DatasetQuestion).where(
        DatasetQuestion.id.in_([i for i in bank_ids if i])))).scalars().all()} if any(bank_ids) else {}
    subject = await session.get(Subject, paper.subject_id)

    def item(q: QuestionPaperQuestion) -> dict:
        source = bank.get(getattr(q, "dataset_question_id", None))
        qtype = str(_v(getattr(q, "question_type", None)) or "").upper()
        options = getattr(q, "options", None) or (source.options if source else None)
        answer = getattr(q, "answer", None) or (source.answer if source else None)
        # the model can't see a figure: tell it what the picture shows, so the key can refer to it
        shown = figure_caption(source) if source else None
        text = f"{q.text}\n[Figure shown with this question: {shown}]" if shown else q.text
        return {"id": str(q.id), "text": text, "marks": q.marks, "bloom_level": _v(q.bloom_level),
                "question_type": qtype if qtype in _TYPES else None, "options": options or None,
                "reference_answer": answer or None,
                "reference_solution": (source.solution if source else None) or None}

    result = await client.answer_key({
        "board": paper.board, "grade": paper.grade, "subject": subject.name if subject else "Science",
        "items": [item(q) for q in targets],
    })
    by_id = {str(q.id): q for q in targets}
    for out in result.get("items", []):
        q = by_id.get(out.get("id"))
        points = [{"point": p["point"], "marks": p["marks"]} for p in out.get("marking_points", [])]
        if q is None or not points or not marks_match(points, q.marks):
            continue  # never store a scheme that doesn't add up
        key = keys.get(q.id)
        if key is None:
            key = QuestionPaperAnswerKey(question_id=q.id)
            session.add(key)
        key.answer, key.marking_points, key.updated_by = out["answer"], points, None
    await session.commit()
    return await get_answer_key(session, paper)


async def update_answer_key(session: AsyncSession, paper: QuestionPaper, question_id: str,
                            body: UpdateAnswerKeyIn, user: User) -> AnswerKeyItemOut:
    q = (await session.execute(select(QuestionPaperQuestion).options(undefer_group(COPILOT_COLUMNS)).where(
        QuestionPaperQuestion.id == question_id, QuestionPaperQuestion.paper_id == paper.id))).scalar_one_or_none()
    if q is None:
        raise NotFoundError(f'No question "{question_id}" on this paper.')
    points = [p.model_dump() for p in body.marking_points] if body.marking_points is not None else None
    if points is not None and not marks_match(points, q.marks):
        raise ValidationError(f"Marking points must add up to the question's {q.marks} marks.")
    key = await session.get(QuestionPaperAnswerKey, q.id)
    if key is None:
        if body.answer is None:
            raise ValidationError("This question has no answer key yet - send an answer.")
        key = QuestionPaperAnswerKey(question_id=q.id, answer=body.answer,
                                     marking_points=points or [{"point": body.answer, "marks": q.marks}])
        session.add(key)
    else:
        if body.answer is not None:
            key.answer = body.answer
        if points is not None:
            key.marking_points = points
    key.updated_by = user.id
    await session.commit()
    return _item(q, key)
