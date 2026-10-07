"""Paper Copilot (C12): chat / voice / quick actions -> validated edit
proposals (C11 ops), and explain-this-question. Nothing here changes the
paper's questions - the app accepts proposals through /actions (W5-01).

The rules check (C10) and its preview belong to W5-01's
src.services.paper_rules; until that module is on this branch both
adapters below return None.
"""

import base64
import binascii
import inspect
import logging
import uuid
from datetime import datetime, timezone

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import AppError, ConflictError, ValidationError
from src.api.schemas.paper_copilot import (
    AfterSummary, EditOp, ExplainOut, NewQuestion, PaperChatIn, PaperChatMessageOut, PaperChatOut, PaperOp,
    Proposal, QuestionSummary, ReorderOp, ReplaceOp,
)
from src.domain.models import (
    BloomLevel, Chapter, Dataset, DatasetQuestion, DatasetType, QuestionPaper, QuestionPaperChapter,
    QuestionPaperDatasetShare, QuestionPaperQuestion, QuestionPaperSection, QuestionPaperStatus, QuestionPaperTopic,
    Subject, Topic, User,
)
from src.domain.models.paper_chat import PaperChatMessage
from src.repositories.dataset_access import visible_datasets
from src.services import paper_copilot_client
from src.services.question_selector import text_only_filter

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 5 * 1024 * 1024
HISTORY_LIMIT = 20
BANK_LIMIT = 30
FRIENDLY_FAILURE = "Sorry, I couldn't work that out just now. Please try again, or say it a little differently."
QUICK_ACTION_LABELS = {
    "harder": "Make it harder", "easier": "Make it easier", "more_application": "More application questions",
    "balance_chapters": "Balance the chapters", "simplify_wording": "Simplify the wording",
    "fix_total_marks": "Fix the total marks",
}
B = BloomLevel
_HIGHER = [B.APPLY, B.ANALYZE, B.EVALUATE, B.CREATE]
_QUICK_ACTION_BLOOMS = {"harder": _HIGHER, "more_application": [B.APPLY, B.ANALYZE], "easier": [B.REMEMBER, B.UNDERSTAND]}
_MESSAGE_BLOOM_WORDS = {"harder": _HIGHER, "difficult": _HIGHER, "tough": _HIGHER, "easier": [B.REMEMBER, B.UNDERSTAND],
                        "recall": [B.REMEMBER], "remember": [B.REMEMBER], "understand": [B.UNDERSTAND],
                        "appl": [B.APPLY], "analy": [B.ANALYZE], "evaluat": [B.EVALUATE], "creat": [B.CREATE]}
_BLOOM_TESTS = {
    B.REMEMBER: "recall a fact, term or definition",
    B.UNDERSTAND: "explain an idea in their own words",
    B.APPLY: "use a rule, formula or idea in a new situation",
    B.ANALYZE: "break a situation into parts and see how they connect",
    B.EVALUATE: "judge a claim or method and justify it with reasons",
    B.CREATE: "put ideas together to design or propose something new",
}
_OPS = TypeAdapter(PaperOp)


def _val(v):
    return v.value if hasattr(v, "value") else v


# ------------------------------------------------------------- W5-01 adapters
async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _jsonable(rc):
    return rc.model_dump(mode="json", by_alias=True) if hasattr(rc, "model_dump") else rc


async def _rules_check(session: AsyncSession, paper: QuestionPaper) -> dict | None:
    try:
        from src.services.paper_rules import rules_check  # W5-01
    except ImportError:
        return None
    return _jsonable(await _maybe_await(rules_check(session, paper)))


async def _preview_rules_check(session: AsyncSession, paper: QuestionPaper, ops: list) -> dict | None:
    """C10 as if `ops` were applied - W5-01's preview, when present."""
    try:
        from src.services.paper_rules import preview_rules_check  # W5-01
    except ImportError:
        return None
    return _jsonable(await _maybe_await(preview_rules_check(session, paper, ops)))


# ------------------------------------------------------------- context
async def _questions(session: AsyncSession, paper: QuestionPaper) -> list[QuestionPaperQuestion]:
    return list((await session.execute(
        select(QuestionPaperQuestion).where(QuestionPaperQuestion.paper_id == paper.id)
        .order_by(QuestionPaperQuestion.order))).scalars().all())


async def _chapters(session: AsyncSession, paper: QuestionPaper, questions) -> dict[str, str]:
    """id -> name of every chapter in the paper's scope (blueprint chapters,
    chapters of its topics, chapters its questions are on)."""
    ids = set((await session.execute(
        select(QuestionPaperChapter.chapter_id).where(QuestionPaperChapter.paper_id == paper.id))).scalars().all())
    ids |= set((await session.execute(
        select(Topic.chapter_id).join(QuestionPaperTopic, QuestionPaperTopic.topic_id == Topic.id)
        .where(QuestionPaperTopic.paper_id == paper.id))).scalars().all())
    ids |= {q.chapter_id for q in questions if getattr(q, "chapter_id", None)}
    if not ids:
        return {}
    rows = (await session.execute(select(Chapter.id, Chapter.name).where(Chapter.id.in_(ids)))).all()
    return {str(i): name for i, name in rows}


def _question_json(q: QuestionPaperQuestion, number: int, chapters: dict[str, str]) -> dict:
    chapter_id = str(q.chapter_id) if getattr(q, "chapter_id", None) else None
    return {"id": str(q.id), "number": number, "text": q.text, "question_type": _val(getattr(q, "question_type", None)),
            "marks": q.marks, "bloom_level": _val(q.bloom_level), "difficulty": _val(getattr(q, "difficulty", None)),
            "chapter_id": chapter_id, "chapter_name": chapters.get(chapter_id or ""),
            "section_name": getattr(q, "section_name", None)}


async def _dataset_ids(session: AsyncSession, paper: QuestionPaper, user: User) -> list:
    """The paper's shared QA banks, else every QA bank the teacher may use for its subject + grade."""
    shared = (await session.execute(
        select(QuestionPaperDatasetShare.dataset_id).join(Dataset, Dataset.id == QuestionPaperDatasetShare.dataset_id)
        .where(QuestionPaperDatasetShare.paper_id == paper.id, Dataset.type == DatasetType.QA))).scalars().all()
    if shared:
        return list(shared)
    return [d.id for d in await visible_datasets(session, user, subject_id=paper.subject_id, grade=paper.grade)
            if d.type == DatasetType.QA]


def _preferred_blooms(message: str | None, quick_action: str | None) -> list[BloomLevel]:
    if quick_action:
        return _QUICK_ACTION_BLOOMS.get(quick_action, [])
    lowered = (message or "").lower()
    return list(dict.fromkeys(b for word, blooms in _MESSAGE_BLOOM_WORDS.items() if word in lowered for b in blooms))


async def bank_candidates(session: AsyncSession, paper: QuestionPaper, user: User, questions, chapter_ids,
                          *, blooms: list[BloomLevel] = (), question_type=None, limit: int = BANK_LIMIT
                          ) -> list[DatasetQuestion]:
    """Bank questions on the paper's chapters it doesn't already use: the
    requested Bloom levels (and type) first, then the rest, at random."""
    dataset_ids = await _dataset_ids(session, paper, user)
    if not dataset_ids or not chapter_ids:
        return []
    query = select(DatasetQuestion).where(
        DatasetQuestion.dataset_id.in_(dataset_ids), DatasetQuestion.chapter_id.in_(list(chapter_ids)),
        text_only_filter())
    if question_type is not None:
        query = query.where(DatasetQuestion.question_type == question_type)
    if blooms:
        query = query.order_by(case((DatasetQuestion.bloom_level.in_(blooms), 0), else_=1))
    rows = (await session.execute(query.order_by(func.random()).limit(limit * 3))).scalars().all()

    used_texts = {q.text.strip().lower() for q in questions}
    used_ids = {str(getattr(q, "dataset_question_id", None)) for q in questions}
    seen, out = set(), []
    for row in rows:
        key = row.external_uid or str(row.id)
        if key in seen or str(row.id) in used_ids or row.text.strip().lower() in used_texts:
            continue
        seen.add(key)
        out.append(row)
    return out[:limit]


def _candidate_json(c: DatasetQuestion) -> dict:
    return {"id": str(c.id), "text": c.text, "question_type": _val(c.question_type),
            "marks": c.marks, "bloom_level": _val(c.bloom_level), "difficulty": _val(c.difficulty),
            "chapter_id": str(c.chapter_id) if c.chapter_id else None,
            "topic_id": str(c.topic_id) if c.topic_id else None, "options": c.options, "answer": c.answer}


# ------------------------------------------------------------- ops -> proposals
def validate_ops(raw_ops: list[dict], questions, chapter_ids: set[str], candidate_ids: set[str],
                 section_names: set[str]) -> list[tuple[PaperOp, str]]:
    """Planner ops that hold against the real paper, with their reasons; the
    rest are dropped with a log line. One op per question at most."""
    ids = {str(q.id) for q in questions}
    sections = {s.lower(): s for s in section_names}
    touched: set[str] = set()
    valid = []
    for raw in raw_ops[:10]:
        problem = None
        try:
            op = _OPS.validate_python(raw)
        except PydanticValidationError as exc:
            logger.info("paper_copilot_op_dropped", extra={"reason": f"invalid shape: {exc.errors()[:1]}"})
            continue
        question_id = getattr(op, "question_id", None)
        new = getattr(op, "new_question", None)
        if question_id is not None and question_id not in ids:
            problem = "unknown questionId"
        elif question_id is not None and question_id in touched:
            problem = "question already changed by another proposal"
        elif isinstance(op, ReorderOp) and (len(op.ordered_question_ids) != len(ids) or set(op.ordered_question_ids) != ids):
            problem = "reorder must list every question exactly once"
        elif isinstance(op, EditOp) and not op.changes.model_dump(exclude_none=True):
            problem = "edit with no changes"
        elif new is not None and new.chapter_id not in chapter_ids:
            problem = "chapter outside the paper"
        elif new is not None and new.source == "bank" and new.dataset_question_id not in candidate_ids:
            problem = "bank question not among the candidates"
        elif getattr(op, "section_name", None) and op.section_name.lower() not in sections:
            problem = "unknown section"
        if problem:
            logger.info("paper_copilot_op_dropped", extra={"op": raw.get("op"), "reason": problem})
            continue
        if new is not None and new.source == "ai":
            new.dataset_question_id = None
        if getattr(op, "section_name", None):
            op.section_name = sections[op.section_name.lower()]
        if question_id:
            touched.add(question_id)
        valid.append((op, str(raw.get("reason") or "")))
    return valid


def _summary(q: QuestionPaperQuestion) -> QuestionSummary:
    return QuestionSummary(question_id=str(q.id), text=q.text, marks=q.marks, bloom_level=q.bloom_level,
                           difficulty=_val(getattr(q, "difficulty", None)))


def to_proposal(op: PaperOp, reason: str, by_id: dict) -> Proposal:
    q = by_id.get(getattr(op, "question_id", None) or "")
    after = None
    if getattr(op, "new_question", None) is not None:
        n = op.new_question
        after = AfterSummary(text=n.text, marks=n.marks, bloom_level=n.bloom_level, difficulty=n.difficulty,
                             source=n.source)
    elif isinstance(op, EditOp):
        c = op.changes
        after = AfterSummary(text=c.text or q.text, marks=c.marks or q.marks, bloom_level=c.bloom_level or q.bloom_level,
                             difficulty=c.difficulty or _val(getattr(q, "difficulty", None)), source="edited")
    return Proposal(proposal_id=str(uuid.uuid4()), op=op, before=_summary(q) if q is not None else None,
                    after=after, reason=reason)


# ------------------------------------------------------------- chat
def _decode_audio(audio_base64: str) -> None:
    try:
        size = len(base64.b64decode(audio_base64, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("audioBase64 isn't valid base64.") from exc
    if not size or size > MAX_AUDIO_BYTES:
        raise ValidationError("Voice notes can be up to 5 MB.")


async def _context(session: AsyncSession, paper: QuestionPaper):
    questions = await _questions(session, paper)
    chapters = await _chapters(session, paper, questions)
    sections = (await session.execute(
        select(QuestionPaperSection).where(QuestionPaperSection.paper_id == paper.id))).scalars().all()
    return questions, chapters, sections


async def chat(session: AsyncSession, paper: QuestionPaper, user: User, body: PaperChatIn) -> PaperChatOut:
    if paper.status == QuestionPaperStatus.FINALIZED:
        raise ConflictError("This paper is finalized and can't be changed.")
    questions, chapters, sections = await _context(session, paper)
    subject = await session.get(Subject, paper.subject_id)
    subject_name = subject.name if subject is not None else ""

    message, transcript = (body.message or "").strip() or None, None
    if body.audio_base64:
        _decode_audio(body.audio_base64)
        vocabulary = f"{subject_name}, Class {paper.grade}. Chapters: {', '.join(chapters.values())}."[:900]
        try:
            transcript = (await paper_copilot_client.transcribe({
                "audio_base64": body.audio_base64, "mime_type": body.mime_type or "audio/m4a", "prompt": vocabulary,
            }))["text"].strip() or None
        except (AppError, KeyError) as exc:
            logger.warning("paper_copilot_transcribe_failed", extra={"reason": str(exc)})
        if transcript is None:
            return PaperChatOut(transcript=None, reply="Sorry, I couldn't hear that clearly. Please try again or type it.",
                                proposals=[], preview_rules_check=None)
        message = transcript
    if message is None and body.quick_action is None:
        raise ValidationError("Send a message, a voice note or a quick action.")

    history = list(reversed((await session.execute(
        select(PaperChatMessage).where(PaperChatMessage.paper_id == paper.id)
        .order_by(PaperChatMessage.created_at.desc(), PaperChatMessage.role).limit(HISTORY_LIMIT))).scalars().all()))
    session.add(PaperChatMessage(paper_id=paper.id, role="teacher", created_by=user.id,
                                 text=message or QUICK_ACTION_LABELS[body.quick_action],
                                 created_at=datetime.now(timezone.utc)))

    rules = await _rules_check(session, paper)
    candidates = await bank_candidates(session, paper, user, questions, chapters.keys(),
                                       blooms=_preferred_blooms(message, body.quick_action))
    payload = {
        "board": paper.board, "grade": paper.grade, "subject": subject_name,
        "message": message, "quick_action": body.quick_action,
        "total_marks": paper.total_marks, "duration_minutes": paper.duration_minutes,
        "sections": [{"name": s.label, "question_count": s.question_count, "marks_each": s.marks_each} for s in sections],
        "chapters": [{"id": i, "name": n} for i, n in chapters.items()],
        "questions": [_question_json(q, n, chapters) for n, q in enumerate(questions, start=1)],
        "rules_issues": [i.get("message", "") for i in (rules or {}).get("issues", [])][:50],
        "bank_candidates": [_candidate_json(c) for c in candidates],
        "history": [{"role": m.role, "text": m.text[:4000]} for m in history],
    }
    try:
        plan = await paper_copilot_client.plan(payload)
        reply, raw_ops = str(plan["reply"]), list(plan.get("ops") or [])
    except Exception as exc:  # never 500 on a planner failure
        logger.warning("paper_copilot_plan_failed", extra={"reason": str(exc)})
        reply, raw_ops = FRIENDLY_FAILURE, []

    by_id = {str(q.id): q for q in questions}
    valid = validate_ops(raw_ops, questions, set(chapters), {str(c.id) for c in candidates}, {s.label for s in sections})
    proposals = [to_proposal(op, reason, by_id) for op, reason in valid]
    preview = await _preview_rules_check(session, paper, [op for op, _ in valid]) if proposals else rules

    session.add(PaperChatMessage(
        paper_id=paper.id, role="copilot", text=reply, created_by=None, created_at=datetime.now(timezone.utc),
        proposals=[p.model_dump(mode="json", by_alias=True) for p in proposals] or None))
    await session.commit()
    return PaperChatOut(transcript=transcript, reply=reply, proposals=proposals, preview_rules_check=preview)


async def history(session: AsyncSession, paper: QuestionPaper) -> list[PaperChatMessageOut]:
    rows = (await session.execute(
        select(PaperChatMessage).where(PaperChatMessage.paper_id == paper.id)
        # same-instant ties: the teacher's message ("teacher" > "copilot") first
        .order_by(PaperChatMessage.created_at, PaperChatMessage.role.desc()))).scalars().all()
    return [PaperChatMessageOut(id=str(m.id), role=m.role, text=m.text, proposals=m.proposals, created_at=m.created_at)
            for m in rows]


# ------------------------------------------------------------- explain
async def explain(session: AsyncSession, paper: QuestionPaper, user: User, question: QuestionPaperQuestion) -> ExplainOut:
    questions, chapters, _ = await _context(session, paper)
    chapter_id = str(question.chapter_id) if getattr(question, "chapter_id", None) else (
        next(iter(chapters)) if len(chapters) == 1 else None)
    chapter_name = chapters.get(chapter_id or "")
    question_type = getattr(question, "question_type", None)
    difficulty = _val(getattr(question, "difficulty", None))

    kind = f"{question.marks}-mark {str(_val(question_type)).replace('_', ' ').lower() + ' ' if question_type else ''}question"
    why = (f"This is a {kind}{f' from {chapter_name}' if chapter_name else ''}. "
           f"It is at the {question.bloom_level.value.title()} level of Bloom's taxonomy: it checks whether "
           f"students can {_BLOOM_TESTS[question.bloom_level]}.")
    if difficulty:
        why += f" Difficulty: {difficulty.lower()}."

    alternative = None
    scope = [chapter_id] if chapter_id else list(chapters)
    for c in await bank_candidates(session, paper, user, questions, scope, blooms=[question.bloom_level],
                                   question_type=question_type, limit=5):
        if c.bloom_level != question.bloom_level or c.chapter_id is None:
            continue
        op = ReplaceOp(op="replace", question_id=str(question.id), new_question=NewQuestion(
            text=c.text, question_type=c.question_type, marks=question.marks, bloom_level=c.bloom_level,
            difficulty=c.difficulty or difficulty or "MEDIUM", chapter_id=str(c.chapter_id),
            topic_id=str(c.topic_id) if c.topic_id else None, options=c.options, answer=c.answer, source="bank",
            dataset_question_id=str(c.id)))
        alternative = to_proposal(op, "A question-bank alternative at the same level and marks.",
                                  {str(question.id): question})
        break
    return ExplainOut(why=why, bloom_level=question.bloom_level, chapter_name=chapter_name, difficulty=difficulty,
                      alternative=alternative)
