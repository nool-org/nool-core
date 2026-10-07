"""Set B: a parallel paper with the same blueprint and, slot for slot, a
different question of the same section, chapter, Bloom level, marks, type and
difficulty. Bank alternatives first (the paper's QA dataset shares), then the
generation service's verified gap-fill for whatever the bank can't cover. Any
slot left without a genuinely different question fails the whole request -
Set B is never padded with a copy or a made-up question.
"""

from difflib import SequenceMatcher

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.services import paper_actions

from shared.errors import ConflictError
from src.api.routes import question_paper as qp_routes
from src.api.schemas.paper_answer_key import ParallelOut
from src.api.schemas.question_paper import CreateQuestionPaperIn
from src.domain.models import (
    Chapter, Dataset, DatasetQuestion, DatasetType, QuestionPaper, QuestionPaperChapter, QuestionPaperDatasetShare,
    QuestionPaperQuestion, QuestionPaperStatus, QuestionPaperTopic, QuestionType, Subject, Subtopic, Topic, User,
)
from src.domain.models.paper_answer_key import QuestionPaperParallel
from src.domain.models.question_paper import PaperDifficultyLevel
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import count_question_papers
from src.services import paper_answer_key_client as client
from src.services.question_selector import text_only_filter

SIMILARITY_LIMIT = 0.85
BANK_CANDIDATES = 20
MAX_SECTION_CHARS = 12000
MAX_SLOTS_PER_CALL = 30
# What makes two questions the "same slot" - copied as is. Only the ones the
# QuestionPaperQuestion model actually has are used (the paper model is W5-01's).
SLOT_FIELDS = ("section_name", "chapter_id", "topic_id", "question_type", "difficulty")
# ponytail: the paper-level columns not copied from the original
_NOT_COPIED = {"id", "created_at", "updated_at", "created_by", "name", "status"}


def _v(x):
    return getattr(x, "value", x)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def too_similar(text: str, taken: list[str]) -> bool:
    """Same or >= 0.85 similar (difflib ratio, case/space-insensitive) to any taken text."""
    # ponytail: pairwise difflib, quick_ratio prunes - fine for a paper's few dozen questions
    a = _norm(text)
    for t in taken:
        m = SequenceMatcher(None, a, _norm(t))
        if a == _norm(t) or (m.quick_ratio() >= SIMILARITY_LIMIT and m.ratio() >= SIMILARITY_LIMIT):
            return True
    return False


def _enum(enum_cls, value):
    try:
        return enum_cls(str(_v(value)).upper()) if value is not None else None
    except ValueError:
        return None


def _question_fields(text: str, options, answer, *, source: str, dataset_question_id=None) -> dict:
    """Content columns for the new question - options go into the text when the
    model has no options column (same rendering as homework)."""
    has = lambda f: hasattr(QuestionPaperQuestion, f)  # noqa: E731
    if options and not has("options"):
        text += "\n" + "\n".join(f"({'abcd'[i]}) {o}" for i, o in enumerate(options[:4]))
    fields = {"text": text, "options": options or None, "answer": answer, "source": source,
              "dataset_question_id": dataset_question_id}
    return {k: v for k, v in fields.items() if k == "text" or has(k)}


async def _scope_chapter_ids(session: AsyncSession, paper: QuestionPaper) -> list:
    chapters = set((await session.execute(select(QuestionPaperChapter.chapter_id).where(
        QuestionPaperChapter.paper_id == paper.id))).scalars().all())
    chapters |= set((await session.execute(select(Topic.chapter_id).join(
        QuestionPaperTopic, QuestionPaperTopic.topic_id == Topic.id).where(
        QuestionPaperTopic.paper_id == paper.id))).scalars().all())
    return list(chapters)


async def _bank_pick(session, q, *, dataset_ids, chapter_ids, taken) -> DatasetQuestion | None:
    chapter = getattr(q, "chapter_id", None)
    conds = [DatasetQuestion.dataset_id.in_(dataset_ids), DatasetQuestion.bloom_level == q.bloom_level,
             text_only_filter(), or_(DatasetQuestion.marks == q.marks, DatasetQuestion.marks.is_(None)),
             DatasetQuestion.chapter_id == chapter if chapter else DatasetQuestion.chapter_id.in_(chapter_ids)]
    if (qtype := _enum(QuestionType, getattr(q, "question_type", None))) is not None:
        conds.append(DatasetQuestion.question_type == qtype)
    if (difficulty := _enum(PaperDifficultyLevel, getattr(q, "difficulty", None))) is not None:
        conds.append(DatasetQuestion.difficulty == difficulty)
    if getattr(q, "dataset_question_id", None):
        conds.append(DatasetQuestion.id != q.dataset_question_id)
    same_topic = (DatasetQuestion.topic_id == q.topic_id).desc().nulls_last() if getattr(q, "topic_id", None) else None
    order = [o for o in (same_topic, func.random()) if o is not None]
    rows = (await session.execute(select(DatasetQuestion).where(*conds)
                                  .order_by(*order).limit(BANK_CANDIDATES))).scalars().all()
    return next((d for d in rows if not too_similar(d.text, taken)), None)


async def _sections(session, chapter_ids) -> tuple[str, list[dict]]:
    chapters = (await session.execute(select(Chapter).where(Chapter.id.in_(chapter_ids)))).scalars().all()
    sections, used = [], 0
    for topic in (await session.execute(select(Topic).where(Topic.chapter_id.in_(chapter_ids)))).scalars().all():
        subs = (await session.execute(select(Subtopic).where(Subtopic.topic_id == topic.id))).scalars().all()
        for title, text in [(topic.name, topic.text_content), *((s.name, s.text_content) for s in subs)]:
            if text and used < MAX_SECTION_CHARS:
                sections.append({"title": title, "text": text[: MAX_SECTION_CHARS - used]})
                used += len(sections[-1]["text"])
    return ", ".join(c.name for c in chapters), sections


async def _generate(session, paper, slots: list[tuple[int, QuestionPaperQuestion]], *, chapter_ids, taken,
                    picked: dict) -> None:
    """Fills `picked[index]` for the slots the generation service writes a
    different question for, one gap-fill call per chapter (<= 30 slots each)."""
    subject = await session.get(Subject, paper.subject_id)
    groups: dict = {}
    for index, q in slots:
        groups.setdefault(getattr(q, "chapter_id", None), []).append((index, q))
    for chapter, group in groups.items():
        chapter_name, sections = await _sections(session, [chapter] if chapter else chapter_ids)
        if not sections:
            continue  # nothing to ground a question in
        for start in range(0, len(group), MAX_SLOTS_PER_CALL):
            chunk = group[start:start + MAX_SLOTS_PER_CALL]
            payload = {
                "subject": subject.name if subject else "Science", "grade": paper.grade, "chapter": chapter_name,
                "sections": sections,
                "slots": [{"bloom_level": _v(q.bloom_level),
                           "difficulty": _v(_enum(PaperDifficultyLevel, getattr(q, "difficulty", None))
                                            or PaperDifficultyLevel.MEDIUM),
                           **({"question_type": "mcq"}
                              if _enum(QuestionType, getattr(q, "question_type", None)) == QuestionType.MCQ else {})}
                          for _, q in chunk],
                "examples": [{"text": q.text, "answer": getattr(q, "answer", None) or ""} for _, q in chunk[:4]],
                "avoid": list(taken),
            }
            result = await client.gap_fill(payload)
            for out in result.get("questions", []):
                if not 0 <= out.get("slot_index", -1) < len(chunk) or too_similar(out["text"], taken):
                    continue
                index = chunk[out["slot_index"]][0]
                taken.append(out["text"])
                answer = out["answer"] if not out.get("solution") else f"{out['answer']}\n\n{out['solution']}"
                picked[index] = _question_fields(out["text"], out.get("options") or None, answer, source="ai")


async def create_parallel(session: AsyncSession, original: QuestionPaper, user: User, name: str | None) -> ParallelOut:
    originals = await paper_actions.load_questions(session, original.id)  # incl. deferred copilot columns
    if not originals:
        raise ConflictError("This paper has no questions yet - generate it before making a Set B.")
    plan = await get_active_plan(session, user.school_id)
    if plan is not None and plan.question_paper_limit is not None:
        if await count_question_papers(session, user.school_id) >= plan.question_paper_limit:
            raise ConflictError(f"Your plan allows up to {plan.question_paper_limit} Question Papers.")

    dataset_ids = list((await session.execute(
        select(QuestionPaperDatasetShare.dataset_id).join(Dataset, Dataset.id == QuestionPaperDatasetShare.dataset_id)
        .where(QuestionPaperDatasetShare.paper_id == original.id, Dataset.type == DatasetType.QA))).scalars().all())
    chapter_ids = await _scope_chapter_ids(session, original)

    taken = [q.text for q in originals]
    picked: dict[int, dict] = {}
    for index, q in enumerate(originals):
        if not dataset_ids:
            break
        bank = await _bank_pick(session, q, dataset_ids=dataset_ids, chapter_ids=chapter_ids, taken=taken)
        if bank is not None:
            taken.append(bank.text)
            picked[index] = _question_fields(bank.text, bank.options, bank.answer, source="bank",
                                             dataset_question_id=bank.id)
    open_slots = [(i, q) for i, q in enumerate(originals) if i not in picked]
    if open_slots:
        if not client.is_configured():
            raise client.GenerationServiceUnavailableError(
                f"The question bank has no different question for {len(open_slots)} slot(s) and question "
                "generation isn't available right now.")
        await _generate(session, original, open_slots, chapter_ids=chapter_ids, taken=taken, picked=picked)
        missing = len(originals) - len(picked)
        if missing:
            raise ConflictError(f"Couldn't find a different question for {missing} of {len(originals)} slot(s) - "
                                "try again, or add questions to the paper's question bank.")

    copied = {c.key: getattr(original, c.key) for c in QuestionPaper.__mapper__.column_attrs
              if c.key not in _NOT_COPIED}
    paper = QuestionPaper(**copied, name=name or f"{original.name} - Set B", created_by=user.id,
                          status=QuestionPaperStatus.DRAFT)
    session.add(paper)
    await session.flush()
    blueprint = CreateQuestionPaperIn.model_validate(
        (await qp_routes._serialize(session, original)).model_dump())
    blueprint.name = paper.name
    await qp_routes._apply_body(session, paper, blueprint)  # chapters/topics/sections/shares/bloom/difficulty

    for index, q in enumerate(originals):
        slot = {f: getattr(q, f) for f in SLOT_FIELDS if hasattr(QuestionPaperQuestion, f)}
        session.add(QuestionPaperQuestion(paper_id=paper.id, order=q.order, bloom_level=q.bloom_level,
                                          marks=q.marks, **slot, **picked[index]))
    session.add(QuestionPaperParallel(paper_id=paper.id, parallel_of=original.id))
    await session.commit()
    return ParallelOut(paper_id=str(paper.id), name=paper.name)
