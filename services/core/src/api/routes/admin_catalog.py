"""Global catalog CRUD (Subjects, Chapters, Topics, Datasets) - Super
Admin only. These are explicitly modeled as a global catalog shared across
every school (no school_id on any of these tables - SchoolCurriculum is
the per-school enable/disable toggle, and that stays School Admin's,
unchanged). Giving School Admin write access here would silently change
these for every other school too.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_role
from src.api.schemas.admin_catalog import (
    ChapterOut,
    CreateChapterIn,
    CreateDatasetIn,
    CreateDatasetQuestionIn,
    CreateSubjectIn,
    CreateSubtopicIn,
    CreateTopicIn,
    DatasetOut,
    DatasetQuestionOut,
    KgSubjectOut,
    KgTreeOut,
    SubjectOut,
    SubtopicOut,
    SyncFromKgIn,
    SyncFromKgOut,
    SyncFromKgResultOut,
    SyncSubjectFromKgIn,
    SyncSubjectFromKgOut,
    TopicOut,
    UpdateChapterIn,
    UpdateDatasetIn,
    UpdateDatasetQuestionIn,
    UpdateSubjectIn,
    UpdateSubtopicIn,
    UpdateTopicIn,
)
from src.api.schemas.common import ListEnvelope
from src.domain.models import (
    Chapter,
    Dataset,
    DatasetQuestion,
    DatasetType,
    GradeSubject,
    QuestionPaper,
    QuestionType,
    Role,
    School,
    Subject,
    SubjectGrade,
    Subtopic,
    TeacherClassAssignment,
    Topic,
    User,
    VoiceTest,
)
from src.domain.models.question_paper import PaperDifficultyLevel
from src.repositories import audit_repository
from src.services import kg_client, question_bank
from src.services.assets import asset_url
from src.services.realtime import curriculum_updates, sync_progress_channel
from src.services.subject_grades import ensure_subjects_fit_grade, grades_by_subject, set_subject_grades

router = APIRouter(prefix="/api/v1/admin", tags=["admin-catalog"])


# ---- Subjects ---------------------------------------------------------


async def _delete_curriculum(session: AsyncSession, node, *, actor: User, kind: str) -> None:
    """Deletes a subject/chapter/topic/subtopic with its own curriculum subtree.
    Anything else still pointing at it (questions, tests, papers, progress,
    school assignments) is left to the foreign keys, which refuse - reported
    as 409 rather than a 500, and nothing is half-deleted."""
    subject_ids = [node.id] if isinstance(node, Subject) else []
    chapter_ids = [node.id] if isinstance(node, Chapter) else []
    topic_ids = [node.id] if isinstance(node, Topic) else []
    if subject_ids:
        chapter_ids = list((await session.execute(select(Chapter.id).where(Chapter.subject_id.in_(subject_ids)))).scalars())
    if chapter_ids:
        topic_ids = list((await session.execute(select(Topic.id).where(Topic.chapter_id.in_(chapter_ids)))).scalars())
    try:
        async with session.begin_nested():
            if topic_ids:
                await session.execute(delete(Subtopic).where(Subtopic.topic_id.in_(topic_ids)))
            if chapter_ids and not isinstance(node, Topic):
                await session.execute(delete(Topic).where(Topic.chapter_id.in_(chapter_ids)))
            if subject_ids:
                await session.execute(delete(SubjectGrade).where(SubjectGrade.subject_id.in_(subject_ids)))
                await session.execute(delete(GradeSubject).where(GradeSubject.subject_id.in_(subject_ids)))
                await session.execute(delete(Chapter).where(Chapter.subject_id.in_(subject_ids)))
            await session.delete(node)
            await session.flush()
    except IntegrityError as exc:
        match = re.search(r'from table "(\w+)"', str(exc.orig))
        used_by = match.group(1).replace("_", " ") if match else "other records"
        raise ConflictError(f"This {kind} is still used by {used_by} - remove or move those first.") from exc
    await audit_repository.record(session, actor_id=actor.id, action=f"{kind}.deleted", target_type=kind,
                                  target_id=str(node.id))
    await session.commit()


@router.get("/subjects")
async def list_subjects(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SubjectOut]:
    result = await session.execute(select(Subject).order_by(func.lower(Subject.name), Subject.grade.nulls_last()))
    subjects = result.scalars().all()
    grades_map = await grades_by_subject(session, [s.id for s in subjects])
    items = [
        SubjectOut(id=str(s.id), name=s.name, grade=s.grade, grades=grades_map.get(str(s.id), []),
                   kg_subject_name=s.kg_subject_name)
        for s in subjects
    ]
    return ListEnvelope(items=items, total=len(items))


async def _subject_name_taken(session: AsyncSession, name: str, grade: int, *,
                              exclude_subject_id: str | None = None) -> bool:
    """Names are unique per grade: "Science" can exist once for grade 9 and
    once for grade 10 (case-insensitive, so "science" doesn't sneak past)."""
    query = select(Subject.id).where(func.lower(Subject.name) == name.strip().lower(), Subject.grade == grade)
    if exclude_subject_id is not None:
        query = query.where(Subject.id != exclude_subject_id)
    result = await session.execute(query)
    return result.first() is not None


@router.post("/subjects", status_code=201)
async def create_subject(
    body: CreateSubjectIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubjectOut:
    if await _subject_name_taken(session, body.name, body.grade):
        raise ConflictError(f'A subject named "{body.name}" already exists for grade {body.grade}.')

    subject = Subject(name=body.name.strip(), grade=body.grade, kg_subject_name=body.kg_subject_name)
    session.add(subject)
    await session.flush()
    await set_subject_grades(session, subject.id, {body.grade})
    await audit_repository.record(
        session, actor_id=actor.id, action="subject.created", target_type="subject", target_id=str(subject.id),
        detail=f"grades {body.grades}",
    )
    await session.commit()
    await session.refresh(subject)
    return SubjectOut(id=str(subject.id), name=subject.name, grade=subject.grade, grades=[subject.grade],
                      kg_subject_name=subject.kg_subject_name)


@router.patch("/subjects/{subject_id}")
async def update_subject(
    subject_id: str,
    body: UpdateSubjectIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubjectOut:
    result = await session.execute(select(Subject).where(Subject.id == subject_id))
    subject = result.scalar_one_or_none()
    if subject is None:
        raise NotFoundError(f'No subject with id "{subject_id}".')
    if await _subject_name_taken(session, body.name, body.grade, exclude_subject_id=subject_id):
        raise ConflictError(f'A subject named "{body.name}" already exists for grade {body.grade}.')
    if subject.grade is not None and body.grade != subject.grade:
        # its chapters/topics came from that grade's knowledge graph
        # its chapters/topics came from that grade's knowledge graph, and classes/datasets/tests use it as grade N
        users = [(Chapter.subject_id, "chapters"), (TeacherClassAssignment.subject_id, "class assignments"),
                 (Dataset.subject_id, "datasets"), (VoiceTest.subject_id, "tests"),
                 (QuestionPaper.subject_id, "question papers")]
        for column, what in users:
            if (await session.execute(select(column).where(column == subject.id).limit(1))).first():
                raise ConflictError(f"This grade-{subject.grade} subject already has {what} - "
                                    f"create a separate subject for grade {body.grade}.")
    subject.name = body.name.strip()
    subject.grade = body.grade
    subject.kg_subject_name = body.kg_subject_name
    await set_subject_grades(session, subject.id, {body.grade})
    await audit_repository.record(
        session, actor_id=actor.id, action="subject.updated", target_type="subject", target_id=subject_id,
        detail=f"grades {body.grades}",
    )
    await session.commit()
    return SubjectOut(id=str(subject.id), name=subject.name, grade=subject.grade, grades=[subject.grade],
                      kg_subject_name=subject.kg_subject_name)


@router.delete("/subjects/{subject_id}")
async def delete_subject(
    subject_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(select(Subject).where(Subject.id == subject_id))
    subject = result.scalar_one_or_none()
    if subject is not None:
        await _delete_curriculum(session, subject, actor=actor, kind="subject")
    return {"deleted": True}


# ---- Chapters -----------------------------------------------------------

@router.get("/subjects/{subject_id}/chapters")
async def list_chapters(
    subject_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ChapterOut]:
    result = await session.execute(select(Chapter).where(Chapter.subject_id == subject_id)
                                   .order_by(Chapter.order_index.nulls_last(), Chapter.name))
    items = [ChapterOut(id=str(c.id), subject_id=subject_id, name=c.name) for c in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/subjects/{subject_id}/chapters", status_code=201)
async def create_chapter(
    subject_id: str,
    body: CreateChapterIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ChapterOut:
    subject = await session.get(Subject, subject_id)
    if subject is None:
        raise NotFoundError("Subject not found.")
    # a subject has one grade - a hand-added chapter belongs to it too
    chapter = Chapter(subject_id=subject_id, name=body.name, grade=subject.grade)
    session.add(chapter)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="chapter.created", target_type="chapter", target_id=str(chapter.id)
    )
    await session.commit()
    await session.refresh(chapter)
    return ChapterOut(id=str(chapter.id), subject_id=subject_id, name=chapter.name)


@router.patch("/chapters/{chapter_id}")
async def update_chapter(
    chapter_id: str,
    body: UpdateChapterIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ChapterOut:
    result = await session.execute(select(Chapter).where(Chapter.id == chapter_id))
    chapter = result.scalar_one_or_none()
    if chapter is None:
        raise NotFoundError(f'No chapter with id "{chapter_id}".')
    chapter.name = body.name
    await audit_repository.record(
        session, actor_id=actor.id, action="chapter.updated", target_type="chapter", target_id=chapter_id
    )
    await session.commit()
    return ChapterOut(id=str(chapter.id), subject_id=str(chapter.subject_id), name=chapter.name)


@router.delete("/chapters/{chapter_id}")
async def delete_chapter(
    chapter_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(select(Chapter).where(Chapter.id == chapter_id))
    chapter = result.scalar_one_or_none()
    if chapter is not None:
        await _delete_curriculum(session, chapter, actor=actor, kind="chapter")
    return {"deleted": True}


# ---- Topics -------------------------------------------------------------

@router.get("/chapters/{chapter_id}/topics")
async def list_topics(
    chapter_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[TopicOut]:
    result = await session.execute(select(Topic).where(Topic.chapter_id == chapter_id))
    items = [TopicOut(id=str(t.id), chapter_id=chapter_id, name=t.name) for t in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/chapters/{chapter_id}/topics", status_code=201)
async def create_topic(
    chapter_id: str,
    body: CreateTopicIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicOut:
    topic = Topic(chapter_id=chapter_id, name=body.name)
    session.add(topic)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="topic.created", target_type="topic", target_id=str(topic.id)
    )
    await session.commit()
    await session.refresh(topic)
    return TopicOut(id=str(topic.id), chapter_id=chapter_id, name=topic.name)


@router.patch("/topics/{topic_id}")
async def update_topic(
    topic_id: str,
    body: UpdateTopicIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicOut:
    result = await session.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()
    if topic is None:
        raise NotFoundError(f'No topic with id "{topic_id}".')
    topic.name = body.name
    await audit_repository.record(
        session, actor_id=actor.id, action="topic.updated", target_type="topic", target_id=topic_id
    )
    await session.commit()
    return TopicOut(id=str(topic.id), chapter_id=str(topic.chapter_id), name=topic.name)


@router.delete("/topics/{topic_id}")
async def delete_topic(
    topic_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()
    if topic is not None:
        await _delete_curriculum(session, topic, actor=actor, kind="topic")
    return {"deleted": True}


# ---- Subtopics ------------------------------------------------------------

@router.get("/topics/{topic_id}/subtopics")
async def list_subtopics(
    topic_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SubtopicOut]:
    result = await session.execute(select(Subtopic).where(Subtopic.topic_id == topic_id))
    items = [SubtopicOut(id=str(s.id), topic_id=topic_id, name=s.name) for s in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/topics/{topic_id}/subtopics", status_code=201)
async def create_subtopic(
    topic_id: str,
    body: CreateSubtopicIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubtopicOut:
    subtopic = Subtopic(topic_id=topic_id, name=body.name)
    session.add(subtopic)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="subtopic.created", target_type="subtopic", target_id=str(subtopic.id)
    )
    await session.commit()
    await session.refresh(subtopic)
    return SubtopicOut(id=str(subtopic.id), topic_id=topic_id, name=subtopic.name)


@router.patch("/subtopics/{subtopic_id}")
async def update_subtopic(
    subtopic_id: str,
    body: UpdateSubtopicIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubtopicOut:
    result = await session.execute(select(Subtopic).where(Subtopic.id == subtopic_id))
    subtopic = result.scalar_one_or_none()
    if subtopic is None:
        raise NotFoundError(f'No subtopic with id "{subtopic_id}".')
    subtopic.name = body.name
    await audit_repository.record(
        session, actor_id=actor.id, action="subtopic.updated", target_type="subtopic", target_id=subtopic_id
    )
    await session.commit()
    return SubtopicOut(id=str(subtopic.id), topic_id=str(subtopic.topic_id), name=subtopic.name)


@router.delete("/subtopics/{subtopic_id}")
async def delete_subtopic(
    subtopic_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(select(Subtopic).where(Subtopic.id == subtopic_id))
    subtopic = result.scalar_one_or_none()
    if subtopic is not None:
        await _delete_curriculum(session, subtopic, actor=actor, kind="subtopic")
    return {"deleted": True}


# ---- Datasets -------------------------------------------------------------

async def _real_question_count(session: AsyncSession, dataset_id) -> int:
    result = await session.execute(
        select(func.count()).select_from(DatasetQuestion).where(DatasetQuestion.dataset_id == dataset_id)
    )
    return result.scalar_one()


async def _dataset_out(session: AsyncSession, dataset: Dataset) -> DatasetOut:
    real_count = await _real_question_count(session, dataset.id)
    return DatasetOut(
        id=str(dataset.id), name=dataset.name,
        question_count=real_count if real_count > 0 else dataset.question_count,
        description=dataset.description,
        subject_id=str(dataset.subject_id) if dataset.subject_id else None,
        board=dataset.board, grade=dataset.grade, restricted=dataset.restricted, type=dataset.type,
        school_id=str(dataset.school_id) if dataset.school_id else None,
        school_name=(await session.get(School, dataset.school_id)).name if dataset.school_id else None,
    )


@router.get("/datasets")
async def list_datasets(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[DatasetOut]:
    result = await session.execute(select(Dataset))
    items = [await _dataset_out(session, d) for d in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/datasets", status_code=201)
async def create_dataset(
    body: CreateDatasetIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetOut:
    # every catalog dataset belongs to one class - a dataset without subject
    # and grade would be usable by no school (see dataset_access)
    if not body.subject_id or body.grade is None:
        raise ValidationError("Pick the dataset's subject and grade.")
    await ensure_subjects_fit_grade(session, [body.subject_id], body.grade, what=f"a grade-{body.grade} dataset")
    dataset = Dataset(
        name=body.name, question_count=body.question_count, description=body.description,
        subject_id=body.subject_id, board=body.board, grade=body.grade, restricted=body.restricted,
        type=body.type,
    )
    session.add(dataset)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="dataset.created", target_type="dataset", target_id=str(dataset.id)
    )
    await session.commit()
    await session.refresh(dataset)
    return await _dataset_out(session, dataset)


async def _get_dataset(session: AsyncSession, dataset_id: str) -> Dataset:
    result = await session.execute(select(Dataset).where(Dataset.id == dataset_id))
    dataset = result.scalar_one_or_none()
    if dataset is None:
        raise NotFoundError(f'No dataset with id "{dataset_id}".')
    return dataset


async def _get_catalog_dataset_for_write(session: AsyncSession, dataset_id: str) -> Dataset:
    """A school's own question bank is view-only for a Super Admin - it
    belongs to that school, which alone edits it."""
    dataset = await _get_dataset(session, dataset_id)
    if dataset.school_id is not None:
        raise ForbiddenError("This is a school's own question bank - only that school can change it.")
    return dataset


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetOut:
    dataset = await _get_dataset(session, dataset_id)
    return await _dataset_out(session, dataset)


@router.patch("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: str,
    body: UpdateDatasetIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetOut:
    dataset = await _get_catalog_dataset_for_write(session, dataset_id)
    if body.name is not None:
        dataset.name = body.name
    if body.question_count is not None and await _real_question_count(session, dataset.id) == 0:
        # only a dataset with no real questions yet has a hand-typed count
        dataset.question_count = body.question_count
    if body.description is not None:
        dataset.description = body.description
    if "subject_id" in body.model_fields_set:
        dataset.subject_id = body.subject_id
    if "board" in body.model_fields_set:
        dataset.board = body.board
    if "grade" in body.model_fields_set:
        dataset.grade = body.grade
    if body.restricted is not None:
        dataset.restricted = body.restricted
    if body.type is not None:
        dataset.type = body.type
    if not dataset.subject_id or dataset.grade is None:
        raise ValidationError("A dataset needs both a subject and a grade.")
    await ensure_subjects_fit_grade(session, [dataset.subject_id], dataset.grade,
                                    what=f"a grade-{dataset.grade} dataset")
    await audit_repository.record(
        session, actor_id=actor.id, action="dataset.updated", target_type="dataset", target_id=dataset_id
    )
    await session.commit()
    return await _dataset_out(session, dataset)


# ---- Dataset Questions (the real content behind question_count) -----------

def _dataset_question_out(q: DatasetQuestion) -> DatasetQuestionOut:
    return DatasetQuestionOut(
        id=str(q.id), dataset_id=str(q.dataset_id),
        chapter_id=str(q.chapter_id) if q.chapter_id else None,
        topic_id=str(q.topic_id) if q.topic_id else None,
        bloom_level=q.bloom_level, question_type=q.question_type, text=q.text,
        options=q.options, answer=q.answer, created_by=str(q.created_by), created_at=q.created_at,
        figures=[{**f, "url": asset_url(f["blob"])} for f in q.figures] if q.figures else None,
        solution=q.solution, marks=q.marks, format=q.format,
        subtopic_id=str(q.subtopic_id) if q.subtopic_id else None, difficulty=q.difficulty,
    )


@router.get("/datasets/{dataset_id}/questions")
async def list_dataset_questions(
    dataset_id: str,
    chapter_id: Annotated[str | None, Query(alias="chapterId")] = None,
    topic_id: Annotated[str | None, Query(alias="topicId")] = None,
    difficulty: PaperDifficultyLevel | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[DatasetQuestionOut]:
    """One page (`total` = every matching question) - catalog datasets hold thousands."""
    await _get_dataset(session, dataset_id)
    conditions = [DatasetQuestion.dataset_id == dataset_id]
    for column, value in ((DatasetQuestion.chapter_id, chapter_id), (DatasetQuestion.topic_id, topic_id),
                          (DatasetQuestion.difficulty, difficulty)):
        if value is not None:
            conditions.append(column == value)
    total = (await session.execute(select(func.count()).select_from(DatasetQuestion).where(*conditions))).scalar_one()
    result = await session.execute(
        select(DatasetQuestion).where(*conditions)
        .order_by(DatasetQuestion.created_at.desc(), DatasetQuestion.id).limit(limit).offset(offset)
    )
    items = [_dataset_question_out(q) for q in result.scalars().all()]
    return ListEnvelope(items=items, total=total)


@router.post("/datasets/{dataset_id}/questions", status_code=201)
async def create_dataset_question(
    dataset_id: str,
    body: CreateDatasetQuestionIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetQuestionOut:
    dataset = await _get_catalog_dataset_for_write(session, dataset_id)
    question = await question_bank.create_question(session, dataset, body, actor)
    await audit_repository.record(
        session, actor_id=actor.id, action="dataset.question.created",
        target_type="dataset_question", target_id=str(question.id), detail=f"Dataset {dataset_id}",
    )
    await session.commit()
    await session.refresh(question)
    return _dataset_question_out(question)


async def _get_dataset_question(session: AsyncSession, dataset_id: str, question_id: str) -> DatasetQuestion:
    result = await session.execute(
        select(DatasetQuestion).where(
            DatasetQuestion.id == question_id, DatasetQuestion.dataset_id == dataset_id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        raise NotFoundError(f'No question with id "{question_id}" in this dataset.')
    return question


@router.patch("/datasets/{dataset_id}/questions/{question_id}")
async def update_dataset_question(
    dataset_id: str,
    question_id: str,
    body: UpdateDatasetQuestionIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetQuestionOut:
    dataset = await _get_catalog_dataset_for_write(session, dataset_id)
    question = await _get_dataset_question(session, dataset_id, question_id)
    await question_bank.update_question(session, dataset, question, body)
    await question_bank.propagate_catalog_edit(session, dataset, question)
    await audit_repository.record(
        session, actor_id=actor.id, action="dataset.question.updated",
        target_type="dataset_question", target_id=question_id,
    )
    await session.commit()
    await session.refresh(question)
    return _dataset_question_out(question)


@router.delete("/datasets/{dataset_id}/questions/{question_id}")
async def delete_dataset_question(
    dataset_id: str,
    question_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    await _get_catalog_dataset_for_write(session, dataset_id)
    result = await session.execute(
        select(DatasetQuestion).where(
            DatasetQuestion.id == question_id, DatasetQuestion.dataset_id == dataset_id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        return {"deleted": True}
    await session.delete(question)
    await session.flush()
    await question_bank.refresh_question_count(session, await _get_dataset(session, dataset_id))
    await audit_repository.record(
        session, actor_id=actor.id, action="dataset.question.deleted",
        target_type="dataset_question", target_id=question_id,
    )
    await session.commit()
    return {"deleted": True}


# ---- Knowledge Graph Sync --------------------------------------------------

@router.get("/curriculum/kg-tree", response_model=KgTreeOut)
async def get_kg_tree(
    dataset_id: str = Query(alias="datasetId"),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> KgTreeOut:
    """Read-only preview of what's currently in the KG for a dataset's
    board/grade/subject, for the tree-view sync picker - never writes
    anything. A Super Admin checks the chapters/topics they want, then
    POSTs those refs to /curriculum/sync-from-kg's include_chapter_refs/
    include_topic_refs.
    """
    dataset = await _get_dataset(session, dataset_id)
    if dataset.type != DatasetType.PRIMARY_CONTENT or not (dataset.board and dataset.grade and dataset.subject_id):
        raise ValidationError(
            "This dataset doesn't name a KG root yet - set type=PRIMARY_CONTENT and board/grade/subject_id first."
        )
    subject = await session.get(Subject, dataset.subject_id)
    if subject is None:
        raise NotFoundError(f'No subject with id "{dataset.subject_id}".')
    tree = await kg_client.get_curriculum_tree(board=dataset.board, grade=dataset.grade, subject=_kg_name(subject))
    if tree is None:
        raise NotFoundError(f'nool-kg has no "{_kg_name(subject)}" content for grade {dataset.grade} yet.')
    return KgTreeOut(board=dataset.board, grade=dataset.grade, subject=tree["subject"], chapters=tree["chapters"])


def _kg_name(subject: Subject) -> str:
    """The name nool-kg actually knows this Subject by - only differs from
    the catalog's own display `name` when kg_subject_name is set (e.g.
    display "Class 10 Science" vs. nool-kg's "Science"). See
    Subject.kg_subject_name's docstring.
    """
    return subject.kg_subject_name or subject.name


async def _sync_chapters_topics_from_kg(
    session: AsyncSession, *, board: str, grade: int, subject: Subject,
    include_chapter_refs: list[str] | None, include_topic_refs: list[str] | None,
    include_subtopic_refs: list[str] | None, job_id: str | None,
    kg_name: str | None = None,
) -> tuple[int, int, int]:
    """The actual sync mechanism, shared by the Dataset-keyed route below
    and the Subject-keyed one (the one the Curriculum page's per-Subject
    "Sync" action actually uses) - upserts Chapter/Topic/Subtopic under
    `subject`, tagged with `grade` (Chapter.grade) so a second sync of the
    same Subject at a different grade never merges into the same flat,
    ungraded chapter list. A subject belongs to one grade, so syncing it
    from another grade's graph is refused. Idempotent via kg_ref, matched
    within this subject (chapters), chapter (topics) and topic (subtopics) -
    every grade's graph numbers from CH01, so refs are only unique there.

    `kg_name` overrides `_kg_name(subject)` for this one call - lets the
    Curriculum page's KG-subject picker (GET /admin/kg-subjects) sync from
    an exact real name on the fly without first saving it onto the
    Subject. include_chapter_refs/include_topic_refs/include_subtopic_refs
    (all None by default = bring in everything the KG has) narrow which
    rows get upserted - what's already in Postgres outside the selection
    is left alone, never removed, so a Super Admin can bring content in
    incrementally as the KG grows. Returns (chapters_synced, topics_synced,
    subtopics_synced).
    """
    if subject.grade is None:
        # a legacy grade-less subject would mix grades' chapters - give it a grade first
        raise ValidationError(f'"{subject.name}" has no grade yet - set its grade before syncing.')
    if grade != subject.grade:
        raise ValidationError(
            f'"{subject.name}" is the grade-{subject.grade} subject - sync grade {grade} into its own subject.')
    tree = await kg_client.get_curriculum_tree(board=board, grade=grade, subject=kg_name or _kg_name(subject))
    if tree is None:
        return 0, 0, 0
    progress = sync_progress_channel(job_id) if job_id else None

    chapters_synced = 0
    topics_synced = 0
    subtopics_synced = 0
    for chapter_data in tree["chapters"]:
        if include_chapter_refs is not None and chapter_data["ref"] not in include_chapter_refs:
            continue
        result = await session.execute(
            select(Chapter).where(Chapter.subject_id == subject.id, Chapter.kg_ref == chapter_data["ref"]))
        chapter = result.scalar_one_or_none()
        if chapter is None:
            chapter = Chapter(kg_ref=chapter_data["ref"], subject_id=subject.id)
            session.add(chapter)
        chapter.name = chapter_data["name"]
        chapter.order_index = chapter_data["number"]
        chapter.grade = grade
        await session.flush()
        chapters_synced += 1
        if progress:
            await progress.broadcast(
                {"type": "chapter.synced", "ref": chapter_data["ref"], "name": chapter_data["name"]}
            )

        for topic_data in chapter_data["topics"]:
            if include_topic_refs is not None and topic_data["ref"] not in include_topic_refs:
                continue
            result = await session.execute(
                select(Topic).where(Topic.chapter_id == chapter.id, Topic.kg_ref == topic_data["ref"]))
            topic = result.scalar_one_or_none()
            if topic is None:
                topic = Topic(kg_ref=topic_data["ref"], chapter_id=chapter.id)
                session.add(topic)
            topic.name = topic_data["name"]
            await session.flush()
            topics_synced += 1
            if progress:
                await progress.broadcast(
                    {"type": "topic.synced", "ref": topic_data["ref"], "name": topic_data["name"]}
                )

            for subtopic_data in topic_data.get("subtopics", []):
                if include_subtopic_refs is not None and subtopic_data["ref"] not in include_subtopic_refs:
                    continue
                result = await session.execute(
                    select(Subtopic).where(Subtopic.topic_id == topic.id, Subtopic.kg_ref == subtopic_data["ref"]))
                subtopic = result.scalar_one_or_none()
                if subtopic is None:
                    subtopic = Subtopic(kg_ref=subtopic_data["ref"], topic_id=topic.id)
                    session.add(subtopic)
                subtopic.name = subtopic_data["name"]
                subtopics_synced += 1
                if progress:
                    await progress.broadcast(
                        {"type": "subtopic.synced", "ref": subtopic_data["ref"], "name": subtopic_data["name"]}
                    )

    return chapters_synced, topics_synced, subtopics_synced


async def _sync_one_dataset_from_kg(
    session: AsyncSession, actor: User, dataset: Dataset, subject: Subject,
    *, include_chapter_refs: list[str] | None, include_topic_refs: list[str] | None,
    include_subtopic_refs: list[str] | None, job_id: str | None,
) -> SyncFromKgResultOut:
    chapters_synced, topics_synced, subtopics_synced = await _sync_chapters_topics_from_kg(
        session, board=dataset.board, grade=dataset.grade, subject=subject,
        include_chapter_refs=include_chapter_refs, include_topic_refs=include_topic_refs,
        include_subtopic_refs=include_subtopic_refs, job_id=job_id,
    )
    await audit_repository.record(
        session, actor_id=actor.id, action="curriculum.synced_from_kg",
        target_type="dataset", target_id=str(dataset.id),
        detail=f"{chapters_synced} chapters, {topics_synced} topics, {subtopics_synced} subtopics",
    )
    return SyncFromKgResultOut(
        dataset_id=str(dataset.id), dataset_name=dataset.name, subject=subject.name,
        board=dataset.board, grade=dataset.grade,
        chapters_synced=chapters_synced, topics_synced=topics_synced, subtopics_synced=subtopics_synced,
    )


@router.post("/curriculum/sync-from-kg")
async def sync_curriculum_from_kg(
    body: SyncFromKgIn | None = None,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SyncFromKgOut:
    """Pulls the current curriculum tree from the kg service (nool-kg-backed,
    built from the ingested NCERT textbook) and upserts Chapter/Topic under
    the right Subject - replacing hand-typed rows with the real thing:
    correct names, correct chapter order (Chapter.order_index), and real
    N.N topic structure.

    Runs once per Dataset of type PRIMARY_CONTENT that names a real KG root
    (board+grade+subject_id all set) - a Dataset *is* that root (see
    Dataset's docstring), so there is no single "the" curriculum to sync
    anymore once more than one board/grade/subject combination has been
    ingested. A QA-type dataset is never a sync target, even if it
    happens to have board/grade/subject_id set - see DatasetType's
    docstring. An empty result list is a legitimate outcome (no
    PRIMARY_CONTENT dataset names a KG root yet), not an error.

    `body` is optional and defaults to "every qualifying dataset, everything
    the KG has" - the original, no-selection behavior. The tree-view sync
    page instead names one `dataset_id` plus the chapter/topic refs the
    Super Admin checked, and a `job_id` to watch live progress on.
    """
    body = body or SyncFromKgIn()
    query = select(Dataset).where(
        Dataset.type == DatasetType.PRIMARY_CONTENT,
        Dataset.subject_id.is_not(None), Dataset.board.is_not(None), Dataset.grade.is_not(None)
    )
    if body.dataset_id is not None:
        query = query.where(Dataset.id == body.dataset_id)
    result = await session.execute(query)
    datasets = result.scalars().all()

    results: list[SyncFromKgResultOut] = []
    for dataset in datasets:
        subject = await session.get(Subject, dataset.subject_id)
        if subject is None:
            continue
        results.append(
            await _sync_one_dataset_from_kg(
                session, actor, dataset, subject,
                include_chapter_refs=body.include_chapter_refs,
                include_topic_refs=body.include_topic_refs,
                include_subtopic_refs=body.include_subtopic_refs,
                job_id=body.job_id,
            )
        )

    await session.commit()

    for r in results:
        await curriculum_updates.broadcast(
            {
                "type": "curriculum.updated",
                "datasetId": r.dataset_id,
                "subject": r.subject,
                "board": r.board,
                "grade": r.grade,
            }
        )

    return SyncFromKgOut(results=results)


_DEFAULT_BOARD = "CBSE"  # the only board nool-kg has ever ingested - see nool_kg_client.py


@router.get("/kg-subjects")
async def list_kg_subjects(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
) -> ListEnvelope[KgSubjectOut]:
    """Real, populated subject/grade combinations nool-kg currently has -
    the Curriculum page's Sync panel shows this list first so a Super
    Admin picks an exact KG subject instead of typing a name that has to
    match nool-kg's own naming exactly. See nool_kg_client.list_subjects's
    docstring for why a wrong typed name is actively harmful, not just
    unhelpful (nool-kg silently creates a stray empty graph for it).
    """
    items = [KgSubjectOut(**s) for s in await kg_client.list_curriculum_subjects()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/subjects/{subject_id}/kg-tree", response_model=KgTreeOut)
async def get_subject_kg_tree(
    subject_id: str,
    grade: int = Query(...),
    kg_subject_name: str | None = Query(default=None, alias="kgSubjectName"),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> KgTreeOut:
    """Read-only preview of what's currently in the KG for this Subject at
    this grade - the Curriculum page's per-Subject "Sync" action opens
    this to render its tree-view picker, then POSTs the checked refs to
    /subjects/{id}/sync-from-kg. `grade` must be one of the Subject's own
    SubjectGrade links (see subject_grades.py) - the same grades already
    shown next to its name in the catalog tree. `kg_subject_name` (from
    the GET /admin/kg-subjects picker) overrides the Subject's own
    kg_subject_name/name for this one preview, without saving anything.
    """
    subject = await session.get(Subject, subject_id)
    if subject is None:
        raise NotFoundError(f'No subject with id "{subject_id}".')
    linked_grades = (await grades_by_subject(session, [subject.id])).get(str(subject.id), [])
    if grade not in linked_grades:
        raise ValidationError(
            f'"{subject.name}" isn\'t linked to grade {grade} - link it from the catalog first.'
        )
    kg_name = kg_subject_name or _kg_name(subject)
    tree = await kg_client.get_curriculum_tree(board=_DEFAULT_BOARD, grade=grade, subject=kg_name)
    if tree is None:
        raise NotFoundError(
            f'nool-kg has no "{kg_name}" content for grade {grade} yet - pick a subject from the list above.'
        )
    return KgTreeOut(board=_DEFAULT_BOARD, grade=grade, subject=tree["subject"], chapters=tree["chapters"])


@router.post("/subjects/{subject_id}/sync-from-kg")
async def sync_subject_from_kg(
    subject_id: str,
    body: SyncSubjectFromKgIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SyncSubjectFromKgOut:
    """The sync a Super Admin actually uses day to day: pick a Subject
    already sitting in the catalog with no chapters yet, pick one of its
    grades, check what to bring in from the tree-view preview above, sync.
    No Dataset involved at all - PRIMARY_CONTENT Datasets remain a
    separate, later step for exposing synced content to schools (see
    question_paper.py's _paper_kg_identity), not a prerequisite for
    getting content into the catalog in the first place.
    """
    subject = await session.get(Subject, subject_id)
    if subject is None:
        raise NotFoundError(f'No subject with id "{subject_id}".')
    linked_grades = (await grades_by_subject(session, [subject.id])).get(str(subject.id), [])
    if body.grade not in linked_grades:
        raise ValidationError(
            f'"{subject.name}" isn\'t linked to grade {body.grade} - link it from the catalog first.'
        )

    chapters_synced, topics_synced, subtopics_synced = await _sync_chapters_topics_from_kg(
        session, board=_DEFAULT_BOARD, grade=body.grade, subject=subject,
        include_chapter_refs=body.include_chapter_refs, include_topic_refs=body.include_topic_refs,
        include_subtopic_refs=body.include_subtopic_refs,
        job_id=body.job_id, kg_name=body.kg_subject_name,
    )
    await audit_repository.record(
        session, actor_id=actor.id, action="curriculum.synced_from_kg",
        target_type="subject", target_id=subject_id,
        detail=f"grade {body.grade}: {chapters_synced} chapters, {topics_synced} topics, "
               f"{subtopics_synced} subtopics",
    )
    await session.commit()

    await curriculum_updates.broadcast(
        {"type": "curriculum.updated", "subjectId": subject_id, "subject": subject.name, "grade": body.grade}
    )

    return SyncSubjectFromKgOut(
        subject_id=subject_id, subject=subject.name, grade=body.grade,
        chapters_synced=chapters_synced, topics_synced=topics_synced, subtopics_synced=subtopics_synced,
    )
