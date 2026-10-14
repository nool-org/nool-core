"""A School Admin's own question banks - datasets owned by the school
(Dataset.school_id), per subject and grade. Only that school's users can
use them (repositories/dataset_access.py); a Super Admin can view them but
never assign them to another school.

Catalog datasets assigned by a Super Admin are readable here too (GET
questions) but only the school's own datasets can be changed.
"""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError, ValidationError
from src.api.deps import get_db_session, require_role
from src.api.routes.admin_catalog import _dataset_question_out
from src.api.schemas.admin_catalog import CreateDatasetQuestionIn, DatasetQuestionOut, UpdateDatasetQuestionIn
from src.api.schemas.common import CamelModel, ListEnvelope
from src.api.schemas.school_admin import BulkImportResultOut, BulkRowResultOut, SchoolDatasetOut
from src.domain.models import (
    BloomLevel, Chapter, Dataset, DatasetQuestion, DatasetType, HomeworkDataset, HomeworkQuestion, ImportJobType,
    QuestionPaperDatasetShare, Role, School, SchoolClass, SchoolDataset, Subject, Topic, User,
)
from src.domain.models.curriculum import Subtopic
from src.domain.models.question_paper import PaperDifficultyLevel
from src.repositories import audit_repository, import_job_repository
from src.repositories.dataset_access import enabled_subject_ids, school_dataset_listing
from src.services import question_bank
from src.services.bulk_import import BulkRowResult, parse_rows
from src.services.subject_grades import ensure_subjects_enabled_for_school, ensure_subjects_fit_grade

router = APIRouter(prefix="/api/v1/school", tags=["school-question-bank"])


class CreateSchoolDatasetIn(CamelModel):
    name: str
    description: str = ""
    subject_id: str
    grade: int


class UpdateSchoolDatasetIn(CamelModel):
    name: str | None = None
    description: str | None = None


async def _out(session: AsyncSession, d: Dataset) -> SchoolDatasetOut:
    subject = await session.get(Subject, d.subject_id) if d.subject_id else None
    return SchoolDatasetOut(
        id=str(d.id), name=d.name, question_count=d.question_count, description=d.description, enabled=True,
        restricted=False, owned=True, subject_id=str(d.subject_id) if d.subject_id else None,
        subject_name=subject.name if subject else None, grade=d.grade,
        subject_enabled=str(d.subject_id) in {str(i) for i in await enabled_subject_ids(session, d.school_id)},
    )


async def _own_dataset(session: AsyncSession, user: User, dataset_id: str, *, write: bool = True) -> Dataset:
    """The school's own bank. For a change (`write`), its subject must also be
    enabled for the school: a disabled subject's banks stay readable but frozen
    until a Super Admin re-enables the subject."""
    dataset = await session.get(Dataset, dataset_id)
    if dataset is None or dataset.school_id != user.school_id:
        # also for a catalog dataset: a school can read those but never change them
        raise NotFoundError(f'No question bank with id "{dataset_id}" owned by your school.')
    if write and str(dataset.subject_id) not in {str(i) for i in await enabled_subject_ids(session, user.school_id)}:
        raise ConflictError("This bank's subject is disabled for your school, so the bank can't be changed "
                            "until a Super Admin enables the subject again.")
    return dataset


async def _own_question(session: AsyncSession, dataset: Dataset, question_id: str) -> DatasetQuestion:
    question = await session.get(DatasetQuestion, question_id)
    if question is None or question.dataset_id != dataset.id:
        raise NotFoundError(f'No question with id "{question_id}" in this question bank.')
    return question


# ---- banks ---------------------------------------------------------------
@router.post("/datasets", status_code=201, summary="Create one of this school's own question banks")
async def create_school_dataset(
    body: CreateSchoolDatasetIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolDatasetOut:
    if await session.get(Subject, body.subject_id) is None:
        raise NotFoundError(f'No subject with id "{body.subject_id}".')
    await ensure_subjects_fit_grade(session, [body.subject_id], body.grade, what=f"a grade-{body.grade} bank")
    await ensure_subjects_enabled_for_school(session, [body.subject_id], user.school_id)
    has_grade = (await session.execute(
        select(SchoolClass.id).where(SchoolClass.school_id == user.school_id, SchoolClass.grade == body.grade).limit(1)
    )).first()
    if has_grade is None:
        raise NotFoundError(f"No class in grade {body.grade} at your school.")
    name = body.name.strip()
    if not name:
        raise ValidationError("A question bank needs a name.")
    school = await session.get(School, user.school_id)
    dataset = Dataset(name=name, description=body.description, question_count=0, restricted=False,
                      type=DatasetType.QA, subject_id=body.subject_id, grade=body.grade,
                      board=school.board if school else None, school_id=user.school_id)
    session.add(dataset)
    await session.flush()
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.created",
                                  target_type="dataset", target_id=str(dataset.id), detail=name)
    await session.commit()
    return await _out(session, dataset)


@router.patch("/datasets/{dataset_id}", summary="Rename / describe one of this school's question banks")
async def update_school_dataset(
    dataset_id: str,
    body: UpdateSchoolDatasetIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolDatasetOut:
    dataset = await _own_dataset(session, user, dataset_id)
    if body.name is not None:
        if not body.name.strip():
            raise ValidationError("A question bank needs a name.")
        dataset.name = body.name.strip()
    if body.description is not None:
        dataset.description = body.description
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.updated",
                                  target_type="dataset", target_id=dataset_id)
    await session.commit()
    return await _out(session, dataset)


@router.delete("/datasets/{dataset_id}", summary="Delete one of this school's question banks")
async def delete_school_dataset(
    dataset_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    dataset = await _own_dataset(session, user, dataset_id)
    in_use = (await session.execute(select(HomeworkDataset.id).where(HomeworkDataset.dataset_id == dataset.id).limit(1))).first() \
        or (await session.execute(select(HomeworkQuestion.id).where(HomeworkQuestion.dataset_id == dataset.id).limit(1))).first() \
        or (await session.execute(select(QuestionPaperDatasetShare.id).where(
            QuestionPaperDatasetShare.dataset_id == dataset.id).limit(1))).first()
    if in_use:
        raise ConflictError("This question bank is used by homework or a question paper, so it can't be deleted.")
    await session.execute(delete(DatasetQuestion).where(DatasetQuestion.dataset_id == dataset.id))
    await session.execute(delete(SchoolDataset).where(SchoolDataset.dataset_id == dataset.id))
    await session.delete(dataset)
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.deleted",
                                  target_type="dataset", target_id=dataset_id)
    await session.commit()
    return {"deleted": True}


# ---- questions -----------------------------------------------------------
@router.get("/datasets/{dataset_id}/questions", summary="Questions of an own or assigned dataset")
async def list_school_dataset_questions(
    dataset_id: str,
    chapter_id: str | None = Query(default=None, alias="chapterId"),
    topic_id: str | None = Query(default=None, alias="topicId"),
    subtopic_id: str | None = Query(default=None, alias="subtopicId"),
    bloom_level: BloomLevel | None = Query(default=None, alias="bloomLevel"),
    difficulty: PaperDifficultyLevel | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[DatasetQuestionOut]:
    # own + assigned, readable even while the subject is disabled (writes are refused then)
    readable = {str(d.id) for d, *_ in await school_dataset_listing(session, user.school_id, include_unassigned=False)}
    if dataset_id not in readable:
        raise NotFoundError(f'No dataset with id "{dataset_id}" available to your school.')
    query = select(DatasetQuestion).where(DatasetQuestion.dataset_id == dataset_id)
    for column, value in ((DatasetQuestion.chapter_id, chapter_id), (DatasetQuestion.topic_id, topic_id),
                          (DatasetQuestion.subtopic_id, subtopic_id), (DatasetQuestion.bloom_level, bloom_level),
                          (DatasetQuestion.difficulty, difficulty)):
        if value is not None:
            query = query.where(column == value)
    if q and q.strip():
        query = query.where(DatasetQuestion.text.ilike(f"%{q.strip()}%"))
    # counted and paged in SQL - a dataset holds thousands of questions
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (await session.execute(
        query.order_by(DatasetQuestion.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return ListEnvelope(items=[_dataset_question_out(row) for row in rows], total=total)


@router.post("/datasets/{dataset_id}/questions", status_code=201)
async def create_school_dataset_question(
    dataset_id: str,
    body: CreateDatasetQuestionIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetQuestionOut:
    dataset = await _own_dataset(session, user, dataset_id)
    question = await question_bank.create_question(session, dataset, body, user, require_chapter=True)
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.question.created",
                                  target_type="dataset_question", target_id=str(question.id))
    await session.commit()
    await session.refresh(question)
    return _dataset_question_out(question)


@router.patch("/datasets/{dataset_id}/questions/{question_id}")
async def update_school_dataset_question(
    dataset_id: str,
    question_id: str,
    body: UpdateDatasetQuestionIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> DatasetQuestionOut:
    dataset = await _own_dataset(session, user, dataset_id)
    question = await _own_question(session, dataset, question_id)
    await question_bank.update_question(session, dataset, question, body, require_chapter=True)
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.question.updated",
                                  target_type="dataset_question", target_id=question_id)
    await session.commit()
    await session.refresh(question)
    return _dataset_question_out(question)


@router.delete("/datasets/{dataset_id}/questions/{question_id}")
async def delete_school_dataset_question(
    dataset_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    dataset = await _own_dataset(session, user, dataset_id)
    question = await _own_question(session, dataset, question_id)
    await session.delete(question)
    await session.flush()
    await question_bank.refresh_question_count(session, dataset)
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.question.deleted",
                                  target_type="dataset_question", target_id=question_id)
    await session.commit()
    return {"deleted": True}


def _match(items, wanted: str):
    """By name, case-insensitive - or by the book's number ("11.6" matches
    "11.6 RESISTANCE OF A SYSTEM OF RESISTORS")."""
    wanted = wanted.strip().lower()
    return next((x for x in items if x.name.strip().lower() == wanted
                 or x.name.strip().lower().startswith(wanted + " ")), None)


@router.post("/datasets/{dataset_id}/questions/bulk-import",
             summary="Bulk-add questions to one of this school's question banks (.csv / .xlsx)")
async def bulk_import_school_dataset_questions(
    dataset_id: str,
    file: UploadFile = File(...),
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BulkImportResultOut:
    """Columns (header row, any order): chapter, topic (optional), subtopic
    (optional), bloomLevel, questionType, difficulty (optional: EASY /
    MEDIUM / HARD), marks (optional), text, options (MCQ / TRUE_FALSE only,
    pipe-separated: "Paris|London|Berlin"), answer, solution (optional).
    Subject and grade come from the question bank itself. chapter/topic/
    subtopic match by name or by number ("11.6")."""
    dataset = await _own_dataset(session, user, dataset_id)
    rows = parse_rows(file.filename or "upload", await file.read())
    chapters = (await session.execute(select(Chapter).where(Chapter.subject_id == dataset.subject_id))).scalars().all()
    chapters = [c for c in chapters if c.grade in (None, dataset.grade)]

    results: list[BulkRowResult] = []
    for index, row in enumerate(rows, start=2):
        def fail(message: str) -> None:
            results.append(BulkRowResult(row=index, status="error", error=message))

        get = lambda key: (row.get(key) or "").strip()  # noqa: E731
        if not all([get("chapter"), get("bloomlevel"), get("questiontype"), get("text"), get("answer")]):
            fail("chapter, bloomLevel, questionType, text and answer are required.")
            continue
        chapter = _match(chapters, get("chapter"))
        if chapter is None:
            fail(f'No chapter "{get("chapter")}" for this subject/grade.')
            continue
        topic = subtopic = None
        if get("topic"):
            topic = _match((await session.execute(select(Topic).where(Topic.chapter_id == chapter.id))).scalars().all(),
                           get("topic"))
            if topic is None:
                fail(f'No topic "{get("topic")}" in {chapter.name}.')
                continue
        if get("subtopic"):
            if topic is None:
                fail("A subtopic needs its topic.")
                continue
            subtopic = _match((await session.execute(select(Subtopic).where(Subtopic.topic_id == topic.id))).scalars().all(),
                              get("subtopic"))
            if subtopic is None:
                fail(f'No subtopic "{get("subtopic")}" in {topic.name}.')
                continue
        try:
            body = CreateDatasetQuestionIn(
                chapter_id=str(chapter.id), topic_id=str(topic.id) if topic else None,
                subtopic_id=str(subtopic.id) if subtopic else None,
                bloom_level=get("bloomlevel").upper(), question_type=get("questiontype").upper(),
                difficulty=get("difficulty").upper() or None, marks=get("marks") or None,
                text=get("text"), answer=get("answer"), solution=get("solution") or None,
                options=[o.strip() for o in get("options").split("|") if o.strip()] or None,
            )
            async with session.begin_nested():  # a bad row rolls back alone
                await question_bank.create_question(session, dataset, body, user, require_chapter=True, recount=False)
        except PydanticValidationError as exc:
            fail("; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors()))
            continue
        except ValidationError as exc:
            fail(str(exc))
            continue
        results.append(BulkRowResult(row=index, status="created"))

    created = sum(r.status == "created" for r in results)
    errors = len(results) - created
    await question_bank.refresh_question_count(session, dataset)
    await audit_repository.record(session, actor_id=user.id, action="school_dataset.questions.bulk_created",
                                  target_type="dataset", target_id=dataset_id, detail=f"{created} created, {errors} failed")
    import_job_repository.record(session, school_id=user.school_id, initiated_by=user.id,
                                 job_type=ImportJobType.CUSTOM_QUESTION, filename=file.filename or "upload",
                                 row_count=len(rows), created_count=created, error_count=errors)
    await session.commit()
    return BulkImportResultOut(results=[BulkRowResultOut(**r.__dict__) for r in results],
                               created_count=created, error_count=errors)
