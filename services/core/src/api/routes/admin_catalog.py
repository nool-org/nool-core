"""Global catalog CRUD (Subjects, Chapters, Topics, Datasets) - Super
Admin only. These are explicitly modeled as a global catalog shared across
every school (no school_id on any of these tables - SchoolCurriculum is
the per-school enable/disable toggle, and that stays School Admin's,
unchanged). Giving School Admin write access here would silently change
these for every other school too.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError, ValidationError

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
    QuestionType,
    Role,
    Subject,
    Subtopic,
    Topic,
    User,
)
from src.repositories import audit_repository
from src.services import kg_client
from src.services.realtime import curriculum_updates, sync_progress_channel
from src.services.subject_grades import grades_by_subject, set_subject_grades

router = APIRouter(prefix="/api/v1/admin", tags=["admin-catalog"])


# ---- Subjects ---------------------------------------------------------

@router.get("/subjects")
async def list_subjects(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SubjectOut]:
    result = await session.execute(select(Subject))
    subjects = result.scalars().all()
    grades_map = await grades_by_subject(session, [s.id for s in subjects])
    items = [
        SubjectOut(id=str(s.id), name=s.name, grades=grades_map.get(str(s.id), []), kg_subject_name=s.kg_subject_name)
        for s in subjects
    ]
    return ListEnvelope(items=items, total=len(items))


async def _subject_name_taken(session: AsyncSession, name: str, *, exclude_subject_id: str | None = None) -> bool:
    query = select(Subject.id).where(Subject.name == name)
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
    if await _subject_name_taken(session, body.name):
        raise ConflictError(f'A subject named "{body.name}" already exists.')

    subject = Subject(name=body.name, kg_subject_name=body.kg_subject_name)
    session.add(subject)
    await session.flush()
    await set_subject_grades(session, subject.id, set(body.grades))
    await audit_repository.record(
        session, actor_id=actor.id, action="subject.created", target_type="subject", target_id=str(subject.id),
        detail=f"grades {body.grades}",
    )
    await session.commit()
    await session.refresh(subject)
    return SubjectOut(id=str(subject.id), name=subject.name, grades=body.grades, kg_subject_name=subject.kg_subject_name)


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
    if await _subject_name_taken(session, body.name, exclude_subject_id=subject_id):
        raise ConflictError(f'A subject named "{body.name}" already exists.')
    subject.name = body.name
    subject.kg_subject_name = body.kg_subject_name
    await set_subject_grades(session, subject.id, set(body.grades))
    await audit_repository.record(
        session, actor_id=actor.id, action="subject.updated", target_type="subject", target_id=subject_id,
        detail=f"grades {body.grades}",
    )
    await session.commit()
    return SubjectOut(id=str(subject.id), name=subject.name, grades=body.grades, kg_subject_name=subject.kg_subject_name)


@router.delete("/subjects/{subject_id}")
async def delete_subject(
    subject_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(select(Subject).where(Subject.id == subject_id))
    subject = result.scalar_one_or_none()
    if subject is not None:
        await session.delete(subject)
        await audit_repository.record(
            session, actor_id=actor.id, action="subject.deleted", target_type="subject", target_id=subject_id
        )
        await session.commit()
    return {"deleted": True}


# ---- Chapters -----------------------------------------------------------

@router.get("/subjects/{subject_id}/chapters")
async def list_chapters(
    subject_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ChapterOut]:
    result = await session.execute(select(Chapter).where(Chapter.subject_id == subject_id))
    items = [ChapterOut(id=str(c.id), subject_id=subject_id, name=c.name) for c in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/subjects/{subject_id}/chapters", status_code=201)
async def create_chapter(
    subject_id: str,
    body: CreateChapterIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ChapterOut:
    chapter = Chapter(subject_id=subject_id, name=body.name)
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
        await session.delete(chapter)
        await audit_repository.record(
            session, actor_id=actor.id, action="chapter.deleted", target_type="chapter", target_id=chapter_id
        )
        await session.commit()
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
        await session.delete(topic)
        await audit_repository.record(
            session, actor_id=actor.id, action="topic.deleted", target_type="topic", target_id=topic_id
        )
        await session.commit()
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
        await session.delete(subtopic)
        await audit_repository.record(
            session, actor_id=actor.id, action="subtopic.deleted", target_type="subtopic", target_id=subtopic_id
        )
        await session.commit()
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
    dataset = await _get_dataset(session, dataset_id)
    if body.name is not None:
        dataset.name = body.name
    if body.question_count is not None:
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
    )


@router.get("/datasets/{dataset_id}/questions")
async def list_dataset_questions(
    dataset_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[DatasetQuestionOut]:
    await _get_dataset(session, dataset_id)
    result = await session.execute(
        select(DatasetQuestion).where(DatasetQuestion.dataset_id == dataset_id).order_by(
            DatasetQuestion.created_at.desc()
        )
    )
    items = [_dataset_question_out(q) for q in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/datasets/{dataset_id}/questions", status_code=201)
async def create_dataset_question(
    dataset_id: str,
    body: CreateDatasetQuestionIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetQuestionOut:
    await _get_dataset(session, dataset_id)
    question = DatasetQuestion(
        dataset_id=dataset_id, chapter_id=body.chapter_id, topic_id=body.topic_id,
        bloom_level=body.bloom_level, question_type=body.question_type, text=body.text,
        options=body.options, answer=body.answer, created_by=actor.id,
    )
    session.add(question)
    await session.flush()
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
    question = await _get_dataset_question(session, dataset_id, question_id)

    if "chapter_id" in body.model_fields_set:
        question.chapter_id = body.chapter_id
    if "topic_id" in body.model_fields_set:
        question.topic_id = body.topic_id
    if body.bloom_level is not None:
        question.bloom_level = body.bloom_level

    question_type = body.question_type or question.question_type
    options = body.options if "options" in body.model_fields_set else question.options
    answer = body.answer or question.answer
    # Inline, not the shared _check_dataset_question_options helper - that
    # one's written to raise plain ValueError for Pydantic's
    # @model_validator to catch (see CreateDatasetQuestionIn), but route
    # code needs shared.errors.ValidationError instead so FastAPI's own
    # error-envelope handler produces the right 422 - same split
    # custom_question.py's update_custom_question already uses.
    if question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
        if not options or len(options) < 2:
            raise ValidationError(f"{question_type.value} questions need at least 2 options.")
        if answer not in options:
            raise ValidationError("answer must exactly match one of the given options.")
    elif options is not None:
        raise ValidationError(f"{question_type.value} questions don't take options - leave it blank.")
    question.question_type = question_type
    question.options = options
    question.answer = answer
    if body.text is not None:
        question.text = body.text

    await audit_repository.record(
        session, actor_id=actor.id, action="dataset.question.updated",
        target_type="dataset_question", target_id=question_id,
    )
    await session.commit()
    return _dataset_question_out(question)


@router.delete("/datasets/{dataset_id}/questions/{question_id}")
async def delete_dataset_question(
    dataset_id: str,
    question_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(
        select(DatasetQuestion).where(
            DatasetQuestion.id == question_id, DatasetQuestion.dataset_id == dataset_id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        return {"deleted": True}
    await session.delete(question)
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
    same Subject at a different grade (e.g. "Science" grade 9 vs 10) never
    merges into the same flat, ungraded chapter list. Idempotent via
    kg_ref - safe to re-run.

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
        result = await session.execute(select(Chapter).where(Chapter.kg_ref == chapter_data["ref"]))
        chapter = result.scalar_one_or_none()
        if chapter is None:
            chapter = Chapter(kg_ref=chapter_data["ref"], subject_id=subject.id)
            session.add(chapter)
        chapter.subject_id = subject.id
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
            result = await session.execute(select(Topic).where(Topic.kg_ref == topic_data["ref"]))
            topic = result.scalar_one_or_none()
            if topic is None:
                topic = Topic(kg_ref=topic_data["ref"], chapter_id=chapter.id)
                session.add(topic)
            topic.chapter_id = chapter.id
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
                result = await session.execute(select(Subtopic).where(Subtopic.kg_ref == subtopic_data["ref"]))
                subtopic = result.scalar_one_or_none()
                if subtopic is None:
                    subtopic = Subtopic(kg_ref=subtopic_data["ref"], topic_id=topic.id)
                    session.add(subtopic)
                subtopic.topic_id = topic.id
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
