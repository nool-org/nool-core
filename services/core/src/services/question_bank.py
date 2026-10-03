"""Writing DatasetQuestion rows - the one set of rules for every editor
(Super Admin catalog datasets, a School Admin's own question banks, and
bulk import): options vs answer, curriculum placement, and the metadata
fields the homework/paper selector filters on (subtopic, difficulty,
marks)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ValidationError
from src.domain.models import Chapter, Dataset, DatasetQuestion, QuestionType, Topic, User
from src.domain.models.curriculum import Subtopic

_CHOICE_TYPES = (QuestionType.MCQ, QuestionType.TRUE_FALSE)
_FIELDS = ("chapter_id", "topic_id", "subtopic_id", "bloom_level", "question_type", "difficulty", "marks",
           "text", "options", "answer", "solution")


def check_options(question_type: QuestionType, options: list[str] | None, answer: str) -> None:
    if question_type in _CHOICE_TYPES:
        if not options or len(options) < 2:
            raise ValidationError(f"{question_type.value} questions need at least 2 options.")
        if answer not in options:
            raise ValidationError("answer must exactly match one of the given options.")
    elif options is not None:
        raise ValidationError(f"{question_type.value} questions don't take options - leave it blank.")


async def check_placement(session: AsyncSession, dataset: Dataset, question: DatasetQuestion) -> None:
    """Chapter within the dataset's subject (and grade, when both are set),
    topic within the chapter, subtopic within the topic. A subtopic alone
    fills in its topic and chapter."""
    if question.subtopic_id:
        subtopic = await session.get(Subtopic, question.subtopic_id)
        if subtopic is None:
            raise ValidationError("Unknown subtopic.")
        if question.topic_id and str(question.topic_id) != str(subtopic.topic_id):
            raise ValidationError("That subtopic isn't in the given topic.")
        question.topic_id = subtopic.topic_id
    if question.topic_id:
        topic = await session.get(Topic, question.topic_id)
        if topic is None:
            raise ValidationError("Unknown topic.")
        if question.chapter_id and str(question.chapter_id) != str(topic.chapter_id):
            raise ValidationError("That topic isn't in the given chapter.")
        question.chapter_id = topic.chapter_id
    if question.chapter_id:
        chapter = await session.get(Chapter, question.chapter_id)
        if chapter is None:
            raise ValidationError("Unknown chapter.")
        if dataset.subject_id and chapter.subject_id != dataset.subject_id:
            raise ValidationError("That chapter isn't in this dataset's subject.")
        if dataset.grade and chapter.grade and chapter.grade != dataset.grade:
            raise ValidationError(f"That chapter is for grade {chapter.grade}, this dataset is grade {dataset.grade}.")


async def create_question(session: AsyncSession, dataset: Dataset, body, actor: User,
                          *, require_chapter: bool = False, recount: bool = True) -> DatasetQuestion:
    """`recount=False` for a bulk import, which recounts once at the end."""
    question = DatasetQuestion(dataset_id=dataset.id, created_by=actor.id, external_uid=new_external_uid(),
                               **{f: getattr(body, f) for f in _FIELDS})
    if question.marks is not None:
        question.marks = float(question.marks)
    check_options(question.question_type, question.options, question.answer)
    await check_placement(session, dataset, question)
    if require_chapter and not question.chapter_id:
        raise ValidationError("A chapter (or a topic/subtopic in it) is required.")
    session.add(question)
    await session.flush()
    if recount:
        await refresh_question_count(session, dataset)
    return question


async def update_question(session: AsyncSession, dataset: Dataset, question: DatasetQuestion, body,
                          *, require_chapter: bool = False) -> DatasetQuestion:
    """PATCH semantics: only fields present in the request change; setting a
    coarser placement (chapter/topic) clears the finer one below it."""
    sent = body.model_fields_set
    for field in _FIELDS:
        if field in sent and (getattr(body, field) is not None or field in (
                "chapter_id", "topic_id", "subtopic_id", "options", "difficulty", "marks", "solution")):
            setattr(question, field, getattr(body, field))
    if "chapter_id" in sent and "topic_id" not in sent:
        question.topic_id = question.subtopic_id = None
    elif "topic_id" in sent and "subtopic_id" not in sent:
        question.subtopic_id = None
    check_options(question.question_type, question.options, question.answer)
    await check_placement(session, dataset, question)
    if require_chapter and not question.chapter_id:
        raise ValidationError("A chapter (or a topic/subtopic in it) is required.")
    return question


async def propagate_catalog_edit(session: AsyncSession, dataset: Dataset, question: DatasetQuestion) -> None:
    """A Super Admin edit of a catalog question applies to every copy of it
    (the same external_uid in this subject's other catalog datasets - e.g. a
    question that is both "Important" and "Previous Year"), and marks them all
    `edited` so scripts/import_dataset.py never overwrites them on a re-import."""
    copies = [question]
    if question.external_uid:
        copies += (await session.execute(
            select(DatasetQuestion).join(Dataset, Dataset.id == DatasetQuestion.dataset_id).where(
                DatasetQuestion.external_uid == question.external_uid, DatasetQuestion.id != question.id,
                Dataset.school_id.is_(None), Dataset.subject_id == dataset.subject_id,
            ))).scalars().all()
    for copy in copies:
        for field in _FIELDS:
            setattr(copy, field, getattr(question, field))
        copy.source = {**(copy.source or {}), "edited": True}  # new dict: JSONB change tracking


async def refresh_question_count(session: AsyncSession, dataset: Dataset) -> None:
    dataset.question_count = (await session.execute(
        select(func.count()).select_from(DatasetQuestion).where(DatasetQuestion.dataset_id == dataset.id)
    )).scalar_one()


def new_external_uid() -> str:
    """Stable id for a hand-written question - the selector de-duplicates on it."""
    return f"manual:{uuid.uuid4()}"
