"""Paper Copilot structured edits (C11) and version history.

Nothing rewrites a paper freely: every change is a list of ops
(replace/add/remove/edit/reorder) that this module validates against the
paper and applies atomically, then snapshots as a new version.

Public API (W5-02 imports these):
    await apply_ops(session, paper, ops, *, created_by, summary, origin) -> PaperActionsOut
        validates + applies + writes a version; caller commits. Any bad op
        raises (ValidationError/NotFoundError/ConflictError) with nothing applied.
    await preview_ops(session, paper, ops) -> RulesCheckOut
        same validation, applied inside a savepoint that is rolled back.
    await restore_version(session, paper, version_id, *, created_by) -> PaperActionsOut
    await create_version(session, paper, *, created_by, summary, origin) -> QuestionPaperVersion
    await load_questions(session, paper_id) -> list[QuestionPaperQuestion]  (ordered, all columns)
    question_out(q) -> QuestionPaperQuestionOut
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer_group

from shared.errors import ConflictError, NotFoundError, ValidationError

from src.api.schemas.question_paper import (
    AddOp,
    EditOp,
    NewQuestionIn,
    PaperActionsOut,
    QuestionPaperQuestionOut,
    RemoveOp,
    ReorderOp,
    ReplaceOp,
    RulesCheckOut,
)
from src.domain.models import (
    Chapter,
    DatasetQuestion,
    QuestionPaper,
    QuestionPaperChapter,
    QuestionPaperSection,
    QuestionPaperStatus,
    QuestionPaperValidation,
    QuestionType,
    Topic,
)
from src.domain.models.question_paper import (
    COPILOT_COLUMNS,
    QuestionPaperQuestion,
    QuestionPaperVersion,
    QuestionSource,
)
from src.services.paper_rules import rules_check
from src.services.question_selector import duplicates_of

MCQ_OPTION_COUNT = 4
_SNAPSHOT_FIELDS = ("bloom_level", "marks", "text", "question_type", "difficulty", "chapter_id", "topic_id",
                    "section_name", "options", "answer", "source", "dataset_question_id")


def ensure_editable(paper: QuestionPaper) -> None:
    if paper.status == QuestionPaperStatus.FINALIZED:
        raise ConflictError("This paper is finalized and can't be changed.")


async def load_questions(session: AsyncSession, paper_id) -> list[QuestionPaperQuestion]:
    result = await session.execute(
        select(QuestionPaperQuestion)
        .options(undefer_group(COPILOT_COLUMNS))
        .where(QuestionPaperQuestion.paper_id == paper_id)
        .order_by(QuestionPaperQuestion.order)
    )
    return list(result.scalars().all())


def question_out(q: QuestionPaperQuestion) -> QuestionPaperQuestionOut:
    return QuestionPaperQuestionOut(
        id=str(q.id), paper_id=str(q.paper_id), order=q.order, bloom_level=q.bloom_level, marks=q.marks,
        text=q.text, question_type=q.question_type, difficulty=q.difficulty,
        chapter_id=str(q.chapter_id) if q.chapter_id else None, topic_id=str(q.topic_id) if q.topic_id else None,
        section_name=q.section_name, options=q.options, answer=q.answer, source=q.source or QuestionSource.AI.value,
        dataset_question_id=str(q.dataset_question_id) if q.dataset_question_id else None,
    )


def _snapshot(questions: list[QuestionPaperQuestion]) -> list[dict]:
    return [question_out(q).model_dump(mode="json") for q in questions]


async def create_version(
    session: AsyncSession, paper: QuestionPaper, *, created_by, summary: str, origin: str
) -> QuestionPaperVersion:
    """Snapshots the paper's current questions as the next version number.
    Locks the paper row so two concurrent saves can't take the same number."""
    await session.execute(select(QuestionPaper.id).where(QuestionPaper.id == paper.id).with_for_update())
    await session.flush()
    last = (await session.execute(
        select(func.max(QuestionPaperVersion.number)).where(QuestionPaperVersion.paper_id == paper.id)
    )).scalar()
    version = QuestionPaperVersion(
        paper_id=paper.id, number=(last or 0) + 1, summary=summary, origin=origin, created_by=created_by,
        snapshot=_snapshot(await load_questions(session, paper.id)),
    )
    session.add(version)
    await session.flush()
    await refresh_validation(session, paper)
    return version


async def refresh_validation(session: AsyncSession, paper: QuestionPaper) -> None:
    """Recomputes the paper's stored marks-accounted / quality / duplicates /
    coverage from its questions as they are now, so the review tiles and the
    finalized header follow every edit (T-039). Runs with each new version."""
    validation = await session.get(QuestionPaperValidation, paper.id)
    if validation is None:
        return
    questions = await load_questions(session, paper.id)
    sections = (await session.execute(
        select(QuestionPaperSection).where(QuestionPaperSection.paper_id == paper.id))).scalars().all()
    wanted = set((await session.execute(
        select(QuestionPaperChapter.chapter_id).where(QuestionPaperChapter.paper_id == paper.id))).scalars().all())
    total, slots = len(questions), sum(s.question_count for s in sections)
    duplicates = len(duplicates_of([(q.text, q.options, q.answer) for q in questions]))
    marks = sum(q.marks for q in questions)
    if wanted:
        validation.coverage_percent = round(100 * len(wanted & {q.chapter_id for q in questions}) / len(wanted))
    validation.marks_accounted_for = marks
    if slots:  # no blueprint sections: generation's own numbers stand
        quality = min(max(0, 100 - duplicates * 20), validation.coverage_percent) if total else 0
        validation.duplicate_count = duplicates
        validation.quality_percent = min(quality, round(100 * total / slots)) if total else 0
    issues = [i for i in validation.issues or [] if "add up to" not in i]
    if paper.total_marks and total and marks != paper.total_marks:
        issues.append(f"The questions add up to {marks} of the paper's {paper.total_marks} marks.")
    validation.issues = issues


# ---------------------------------------------------------------- validation
class _Ctx:
    """What ops are validated against, loaded once per call."""

    def __init__(self, paper, questions, section_labels):
        self.paper = paper
        self.questions = questions  # ordered, mutated as ops apply
        self.section_labels = section_labels

    def find(self, question_id: str) -> QuestionPaperQuestion:
        for q in self.questions:
            if str(q.id) == str(question_id):
                return q
        raise NotFoundError(f'No question "{question_id}" on this paper.')


def _check_marks(marks: int) -> None:
    if marks <= 0:
        raise ValidationError("Marks must be greater than 0.")


def _check_options(question_type, options) -> None:
    if question_type == QuestionType.MCQ and (
        not options or len(options) != MCQ_OPTION_COUNT or any(not str(o).strip() for o in options)
    ):
        raise ValidationError(f"An MCQ needs exactly {MCQ_OPTION_COUNT} non-empty options.")


async def _check_new_question(session: AsyncSession, ctx: _Ctx, nq: NewQuestionIn) -> None:
    _check_marks(nq.marks)
    _check_options(nq.question_type, nq.options)
    try:
        chapter_id = uuid.UUID(nq.chapter_id)
        topic_id = uuid.UUID(nq.topic_id) if nq.topic_id else None
        dq_id = uuid.UUID(nq.dataset_question_id) if nq.dataset_question_id else None
    except ValueError:
        raise ValidationError("chapterId/topicId/datasetQuestionId must be UUIDs.")
    chapter = await session.get(Chapter, chapter_id)
    if chapter is None or chapter.subject_id != ctx.paper.subject_id:
        raise ValidationError("The question's chapter must belong to the paper's subject.")
    if topic_id is not None:
        topic = await session.get(Topic, topic_id)
        if topic is None or topic.chapter_id != chapter_id:
            raise ValidationError("The question's topic must belong to its chapter.")
    if dq_id is not None and await session.get(DatasetQuestion, dq_id) is None:
        raise ValidationError(f'No bank question "{nq.dataset_question_id}".')


def _new_row(paper: QuestionPaper, nq: NewQuestionIn, section_name: str | None) -> QuestionPaperQuestion:
    return QuestionPaperQuestion(
        id=uuid.uuid4(), paper_id=paper.id, order=0, bloom_level=nq.bloom_level, marks=nq.marks, text=nq.text,
        question_type=nq.question_type, difficulty=nq.difficulty, chapter_id=uuid.UUID(nq.chapter_id),
        topic_id=uuid.UUID(nq.topic_id) if nq.topic_id else None, section_name=section_name,
        options=nq.options, answer=nq.answer, source=nq.source,
        dataset_question_id=uuid.UUID(nq.dataset_question_id) if nq.dataset_question_id else None,
    )


async def _apply_one(session: AsyncSession, ctx: _Ctx, op) -> None:
    qs = ctx.questions
    if isinstance(op, ReplaceOp):
        old = ctx.find(op.question_id)
        await _check_new_question(session, ctx, op.new_question)
        new = _new_row(ctx.paper, op.new_question, old.section_name)
        qs[qs.index(old)] = new
        session.add(new)
        await session.delete(old)
    elif isinstance(op, AddOp):
        await _check_new_question(session, ctx, op.new_question)
        if op.section_name is not None and op.section_name not in ctx.section_labels:
            raise ValidationError(f'No section "{op.section_name}" on this paper.')
        if op.position is not None and not 1 <= op.position <= len(qs) + 1:
            raise ValidationError(f"position must be between 1 and {len(qs) + 1}.")
        section = op.section_name
        if op.position is not None:
            index = op.position - 1
            if section is None and qs:  # take the section of the slot it lands in
                section = qs[min(index, len(qs) - 1)].section_name
        elif section is not None:
            in_section = [i for i, q in enumerate(qs) if q.section_name == section]
            index = in_section[-1] + 1 if in_section else len(qs)
        else:
            index = len(qs)
        new = _new_row(ctx.paper, op.new_question, section)
        qs.insert(index, new)
        session.add(new)
    elif isinstance(op, RemoveOp):
        old = ctx.find(op.question_id)
        qs.remove(old)
        await session.delete(old)
    elif isinstance(op, EditOp):
        q = ctx.find(op.question_id)
        changes = op.changes.model_dump(exclude_unset=True)
        if not changes:
            raise ValidationError("An edit needs at least one change.")
        if "marks" in changes:
            _check_marks(changes["marks"])
        for field in ("text", "bloom_level", "difficulty", "question_type"):
            if field in changes and changes[field] is None:
                raise ValidationError(f"{field} can't be cleared.")
        _check_options(changes.get("question_type", q.question_type), changes.get("options", q.options))
        for field, value in changes.items():
            setattr(q, field, value)
        q.source = QuestionSource.EDITED.value
    elif isinstance(op, ReorderOp):
        by_id = {str(q.id): q for q in qs}
        if len(op.ordered_question_ids) != len(by_id) or set(op.ordered_question_ids) != set(by_id):
            raise ValidationError("orderedQuestionIds doesn't match the paper's current question set exactly.")
        ctx.questions = [by_id[i] for i in op.ordered_question_ids]


async def _apply(session: AsyncSession, paper: QuestionPaper, ops: list) -> None:
    labels = set((await session.execute(
        select(QuestionPaperSection.label).where(QuestionPaperSection.paper_id == paper.id)
    )).scalars().all())
    ctx = _Ctx(paper, await load_questions(session, paper.id), labels)
    for op in ops:
        await _apply_one(session, ctx, op)
    for order, q in enumerate(ctx.questions, start=1):
        q.order = order
    await session.flush()


async def _result(session, paper, version) -> PaperActionsOut:
    return PaperActionsOut(
        version_id=str(version.id), version_number=version.number,
        questions=[question_out(q) for q in await load_questions(session, paper.id)],
        rules_check=await rules_check(session, paper),
    )


async def apply_ops(
    session: AsyncSession, paper: QuestionPaper, ops: list, *, created_by, summary: str, origin: str
) -> PaperActionsOut:
    ensure_editable(paper)
    async with session.begin_nested():  # a bad op rolls every earlier op back
        await _apply(session, paper, ops)
        version = await create_version(session, paper, created_by=created_by, summary=summary, origin=origin)
    return await _result(session, paper, version)


async def preview_ops(session: AsyncSession, paper: QuestionPaper, ops: list) -> RulesCheckOut:
    """The rules check as if `ops` were applied - nothing is kept."""
    ensure_editable(paper)
    savepoint = await session.begin_nested()
    try:
        await _apply(session, paper, ops)
        return await rules_check(session, paper)
    finally:
        await savepoint.rollback()


async def restore_version(session: AsyncSession, paper: QuestionPaper, version_id: str, *, created_by) -> PaperActionsOut:
    """A NEW version whose questions equal the chosen snapshot. Question ids
    are kept, so anything keyed by question id (answer keys) lines back up."""
    ensure_editable(paper)
    try:
        version_uuid = uuid.UUID(str(version_id))
    except ValueError:
        raise NotFoundError(f'No version "{version_id}" on this paper.')
    source = (await session.execute(select(QuestionPaperVersion).where(
        QuestionPaperVersion.id == version_uuid, QuestionPaperVersion.paper_id == paper.id
    ))).scalar_one_or_none()
    if source is None:
        raise NotFoundError(f'No version "{version_id}" on this paper.')

    async with session.begin_nested():
        current = {str(q.id): q for q in await load_questions(session, paper.id)}
        for item in source.snapshot:
            data = QuestionPaperQuestionOut.model_validate(item)
            q = current.pop(data.id, None)
            if q is None:
                q = QuestionPaperQuestion(id=uuid.UUID(data.id), paper_id=paper.id)
                session.add(q)
            q.order = data.order
            for field in _SNAPSHOT_FIELDS:
                value = getattr(data, field)
                if field.endswith("_id") and value is not None:
                    value = uuid.UUID(value)
                setattr(q, field, value)
        for q in current.values():
            await session.delete(q)
        await session.flush()
        version = await create_version(
            session, paper, created_by=created_by, summary=f"Restored version {source.number}", origin="restore",
        )
    return await _result(session, paper, version)
