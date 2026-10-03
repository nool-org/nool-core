from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.homework import (
    AssignHomeworkIn,
    CreateHomeworkIn,
    HomeworkOut,
    HomeworkQuestionOut,
    UpdateHomeworkQuestionIn,
)
from src.api.schemas.roster import AssignmentTarget
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    DatasetQuestion,
    Feature,
    Homework,
    HomeworkBloomDistribution,
    HomeworkDataset,
    HomeworkQuestion,
    HomeworkStatus,
    HomeworkTargetStudent,
    Role,
    School,
    SchoolClass,
    TeacherClassAssignment,
    StudentHomeworkProgress,
    StudentProfile,
    TestStatus,
    Topic,
    User,
    VoiceTest,
)
from src.repositories.dataset_access import ensure_datasets_visible
from src.repositories.lookups import ensure_students_in_class, get_homework_for_teacher
from src.repositories.roster_repository import teaches_class_subject
from src.services.bloom_defaults import resolve_bloom_targets
from src.services.homework_builder import QuestionDraft, build_homework_questions, grounding_dataset_id, question_uid

# a generate claim older than this is treated as a crashed run
GENERATION_LOCK_MINUTES = 10

router = APIRouter(prefix="/api/v1", tags=["homework"])


async def _serialize(session: AsyncSession, hw: Homework) -> HomeworkOut:
    datasets = await session.execute(
        select(HomeworkDataset.dataset_id).where(HomeworkDataset.homework_id == hw.id)
    )
    dataset_ids = [str(row[0]) for row in datasets.all()]

    distribution = await session.execute(
        select(HomeworkBloomDistribution).where(HomeworkBloomDistribution.homework_id == hw.id)
    )
    from src.api.schemas.bloom import BloomTarget

    bloom_distribution = [
        BloomTarget(level=d.bloom_level, value=d.value) for d in distribution.scalars().all()
    ]

    student_ids: list[str] | None = None
    student_names: list[str] | None = None
    if hw.target_mode == AssignmentTargetMode.SPECIFIC_STUDENTS:
        targets = (await session.execute(
            select(HomeworkTargetStudent.student_id, User.display_name)
            .join(User, User.id == HomeworkTargetStudent.student_id)
            .where(HomeworkTargetStudent.homework_id == hw.id)
            .order_by(User.display_name)
        )).all()
        student_ids = [str(sid) for sid, _ in targets]
        student_names = [name for _, name in targets]
    chapter_name = (await session.execute(
        select(Chapter.name).join(VoiceTest, VoiceTest.chapter_id == Chapter.id).where(VoiceTest.id == hw.test_id)
    )).scalar_one_or_none()

    return HomeworkOut(
        id=str(hw.id),
        test_id=str(hw.test_id),
        class_id=str(hw.class_id),
        gap_topic=hw.gap_topic,
        gap_mastery_percent=hw.gap_mastery_percent,
        dataset_ids=dataset_ids,
        total_questions=hw.total_questions,
        difficulty=hw.difficulty,
        bloom_distribution=bloom_distribution,
        target=AssignmentTarget(mode=hw.target_mode, student_ids=student_ids),
        completion_window_hours=hw.completion_window_hours,
        status=hw.status,
        assigned_count=hw.assigned_count,
        completed_count=hw.completed_count,
        chapter_name=chapter_name,
        target_student_names=student_names,
    )


@router.get("/homework")
async def list_homework(
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[HomeworkOut]:
    # only homework of classes + subjects this teacher teaches
    result = await session.execute(
        select(Homework)
        .join(SchoolClass, SchoolClass.id == Homework.class_id)
        .join(VoiceTest, VoiceTest.id == Homework.test_id)
        .join(TeacherClassAssignment, and_(
            TeacherClassAssignment.class_id == Homework.class_id,
            TeacherClassAssignment.subject_id == VoiceTest.subject_id,
            TeacherClassAssignment.teacher_id == user.id))
        .where(SchoolClass.school_id == user.school_id)
        .distinct()
    )
    items = [await _serialize(session, hw) for hw in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/homework/{homework_id}")
async def get_homework(
    homework_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkOut:
    hw = await get_homework_for_teacher(session, homework_id, user)
    return await _serialize(session, hw)


@router.post("/homework", status_code=201, summary="Create (GENERATING)")
async def create_homework(
    body: CreateHomeworkIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkOut:
    test_result = await session.execute(
        select(VoiceTest, SchoolClass.grade)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .where(VoiceTest.id == body.test_id, SchoolClass.school_id == user.school_id)
    )
    row = test_result.one_or_none()
    if row is None:
        raise NotFoundError(f'No Test with id "{body.test_id}" in your school.')
    test, class_grade = row
    if test.status != TestStatus.RESULTS_READY:
        raise ConflictError("Source Test isn't RESULTS_READY.")
    if not await teaches_class_subject(session, user.id, test.class_id, test.subject_id):
        raise ForbiddenError("You don't teach this subject in this class.")
    await ensure_datasets_visible(session, user, body.dataset_ids, subject_id=test.subject_id, grade=class_grade)

    gap = await session.get(Topic, test.topic_id) if test.topic_id else await session.get(Chapter, test.chapter_id)
    hw = Homework(
        test_id=test.id,
        class_id=test.class_id,
        gap_topic=gap.name if gap else "",
        gap_mastery_percent=0,
        total_questions=body.total_questions,
        difficulty=body.difficulty,
        target_mode=AssignmentTargetMode.WHOLE_CLASS,
        status=HomeworkStatus.GENERATING,
    )
    session.add(hw)
    await session.flush()

    for dataset_id in body.dataset_ids:
        session.add(HomeworkDataset(homework_id=hw.id, dataset_id=dataset_id))
    school = await session.get(School, user.school_id)
    for target in resolve_bloom_targets(school, body.bloom_distribution):
        session.add(
            HomeworkBloomDistribution(homework_id=hw.id, bloom_level=target.level, value=target.value)
        )

    await session.commit()
    await session.refresh(hw)
    return await _serialize(session, hw)


@router.post("/homework/{homework_id}/generate")
async def generate_homework(
    homework_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkOut:
    hw = await get_homework_for_teacher(session, homework_id, user)
    # claim the generation under a row lock (short transaction): a second call -
    # double click, or a client retry after a timeout - must not double the questions
    await session.refresh(hw, with_for_update=True)
    if hw.status not in (HomeworkStatus.GENERATING, HomeworkStatus.FAILED):
        raise ConflictError("This homework has already been generated.")
    now = datetime.now(timezone.utc)
    if hw.generation_started_at and now - hw.generation_started_at < timedelta(minutes=GENERATION_LOCK_MINUTES):
        raise ConflictError("This homework is already being generated - it'll be ready shortly.")
    hw.generation_started_at = now

    distribution_rows = (
        await session.execute(
            select(HomeworkBloomDistribution).where(HomeworkBloomDistribution.homework_id == hw.id)
        )
    ).scalars().all()
    bloom_distribution = {d.bloom_level: d.value for d in distribution_rows}
    test = await session.get(VoiceTest, hw.test_id)
    picked = [row[0] for row in (await session.execute(
        select(HomeworkDataset.dataset_id).where(HomeworkDataset.homework_id == hw.id)
    )).all()]
    fallback_dataset_id = await grounding_dataset_id(session, user=user, test=test, picked_dataset_ids=picked)
    if fallback_dataset_id is None:
        hw.generation_started_at = None
        await session.commit()
        raise ConflictError("No question bank is available for this class and subject yet.")
    await session.commit()  # the claim is visible to other requests; the lock is released

    try:
        drafts = await build_homework_questions(
            session, user=user, test=test, total=hw.total_questions, bloom_weights=bloom_distribution,
            difficulty=hw.difficulty, fallback_dataset_id=fallback_dataset_id, picked_dataset_ids=picked or None,
            seed=str(hw.id), release_before_generation=True,
        )
        if not drafts:
            raise ConflictError("Couldn't find or write any question for this homework - try again later.")
    except Exception:
        await session.rollback()
        hw.status, hw.generation_started_at = HomeworkStatus.FAILED, None
        await session.commit()
        raise

    # a retry after FAILED replaces whatever a previous attempt left behind
    await session.execute(delete(HomeworkQuestion).where(HomeworkQuestion.homework_id == hw.id))
    for order, d in enumerate(drafts, start=1):
        session.add(
            HomeworkQuestion(
                homework_id=hw.id,
                order=order,
                bloom_level=d.bloom_level,
                dataset_id=d.dataset_id,
                source_question_id=d.source_question_id,
                text=d.text,
                answer=d.answer,
            )
        )
    hw.total_questions = len(drafts)
    hw.status, hw.generation_started_at = HomeworkStatus.REVIEW, None
    await session.commit()
    await session.refresh(hw)
    return await _serialize(session, hw)


@router.get("/homework/{homework_id}/questions")
async def list_homework_questions(
    homework_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[HomeworkQuestionOut]:
    await get_homework_for_teacher(session, homework_id, user)
    return await _questions_out(session, homework_id)


async def _questions_out(session: AsyncSession, homework_id: str) -> ListEnvelope[HomeworkQuestionOut]:
    result = await session.execute(
        select(HomeworkQuestion)
        .where(HomeworkQuestion.homework_id == homework_id)
        .order_by(HomeworkQuestion.order)
    )
    items = [
        HomeworkQuestionOut(
            id=str(q.id),
            homework_id=homework_id,
            order=q.order,
            bloom_level=q.bloom_level,
            dataset_id=str(q.dataset_id),
            text=q.text,
            answer=q.answer,
        )
        for q in result.scalars().all()
    ]
    return ListEnvelope(items=items, total=len(items))


async def _question_in_review(session: AsyncSession, homework_id: str, question_id: str,
                              user: User) -> tuple[Homework, HomeworkQuestion]:
    """Questions can only change while the teacher reviews the homework -
    once assigned, students may already be answering them."""
    hw = await get_homework_for_teacher(session, homework_id, user)
    if hw.status != HomeworkStatus.REVIEW:
        raise ConflictError("Questions can only be changed while the homework is in review.")
    question = (await session.execute(
        select(HomeworkQuestion).where(HomeworkQuestion.id == question_id, HomeworkQuestion.homework_id == hw.id)
    )).scalar_one_or_none()
    if question is None:
        raise NotFoundError(f'No question "{question_id}" on this Homework.')
    return hw, question


@router.patch("/homework/{homework_id}/questions/{question_id}")
async def update_homework_question(
    homework_id: str,
    question_id: str,
    body: UpdateHomeworkQuestionIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[HomeworkQuestionOut]:
    if body.text is None and body.answer is None:
        raise ValidationError("Send the question's new text, answer, or both.")
    _, question = await _question_in_review(session, homework_id, question_id, user)
    if body.text is not None:
        question.text = body.text
    if body.answer is not None:
        question.answer = body.answer
    await session.commit()
    return await _questions_out(session, homework_id)


@router.delete("/homework/{homework_id}/questions/{question_id}")
async def delete_homework_question(
    homework_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[HomeworkQuestionOut]:
    hw, question = await _question_in_review(session, homework_id, question_id, user)
    remaining = (await session.execute(
        select(HomeworkQuestion).where(HomeworkQuestion.homework_id == hw.id, HomeworkQuestion.id != question.id)
        .order_by(HomeworkQuestion.order)
    )).scalars().all()
    if not remaining:
        raise ConflictError("A homework needs at least one question - replace this one instead.")
    await session.delete(question)
    for order, q in enumerate(remaining, start=1):
        q.order = order
    hw.total_questions = len(remaining)
    await session.commit()
    return await _questions_out(session, homework_id)


@router.post("/homework/{homework_id}/questions/{question_id}/replace")
async def replace_homework_question(
    homework_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[HomeworkQuestionOut]:
    """Swaps one question for a new one at the same Bloom level - from the
    bank first, else generated - with its own answer, never repeating a
    question already in this homework (the replaced one included)."""
    hw, question = await _question_in_review(session, homework_id, question_id, user)
    test = await session.get(VoiceTest, hw.test_id)
    current = (await session.execute(
        select(HomeworkQuestion, DatasetQuestion)
        .outerjoin(DatasetQuestion, DatasetQuestion.id == HomeworkQuestion.source_question_id)
        .where(HomeworkQuestion.homework_id == hw.id)
    )).all()
    keep = [QuestionDraft(bloom_level=q.bloom_level, text=q.text, answer=q.answer, dataset_id=q.dataset_id,
                          source_question_id=q.source_question_id, source_uid=question_uid(src) if src else None)
            for q, src in current]
    picked = [row[0] for row in (await session.execute(
        select(HomeworkDataset.dataset_id).where(HomeworkDataset.homework_id == hw.id))).all()]
    fallback_dataset_id = await grounding_dataset_id(session, user=user, test=test, picked_dataset_ids=picked)
    drafts = await build_homework_questions(
        session, user=user, test=test, total=1, bloom_weights={question.bloom_level: 1}, difficulty=hw.difficulty,
        fallback_dataset_id=fallback_dataset_id or question.dataset_id, picked_dataset_ids=picked or None,
        seed=f"{hw.id}:{question.id}:{len(keep)}", keep=keep,
    )
    if not drafts:
        raise ConflictError("No other question is available for this slot right now.")
    new = drafts[0]
    question.text, question.answer = new.text, new.answer
    question.dataset_id, question.source_question_id = new.dataset_id, new.source_question_id
    await session.commit()
    return await _questions_out(session, homework_id)


@router.post("/homework/{homework_id}/assign")
async def assign_homework(
    homework_id: str,
    body: AssignHomeworkIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.HOMEWORK)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkOut:
    """Fans out to a real `StudentHomeworkProgress` row per targeted
    student - this used to only ever write `HomeworkTargetStudent` (and
    only for SPECIFIC_STUDENTS, never WHOLE_CLASS), so
    GET /me/homework/current - which reads StudentHomeworkProgress, not
    HomeworkTargetStudent - had nothing to find for any student, on any
    Homework, regardless of target mode. Same missing-fan-out shape as
    VoiceTest's WHOLE_CLASS targeting (see test_completion.py's
    docstring) - fixed here for real rather than patched around.
    """
    hw = await get_homework_for_teacher(session, homework_id, user)
    if hw.status != HomeworkStatus.REVIEW:
        raise ConflictError("Only homework that's ready for review can be assigned.")
    hw.target_mode = body.target.mode
    hw.completion_window_hours = body.completion_window_hours
    hw.status = HomeworkStatus.ASSIGNED

    if body.target.mode == AssignmentTargetMode.SPECIFIC_STUDENTS:
        # only students of this homework's class - never another class/school
        target_student_ids = await ensure_students_in_class(session, body.target.student_ids or [], hw.class_id)
        if not target_student_ids:
            raise ValidationError("Pick at least one student.")
        already_targeted = {row[0] for row in (await session.execute(
            select(HomeworkTargetStudent.student_id).where(HomeworkTargetStudent.homework_id == hw.id))).all()}
        for student_id in target_student_ids:
            if student_id not in already_targeted:
                session.add(HomeworkTargetStudent(homework_id=hw.id, student_id=student_id))
    else:
        roster = await session.execute(
            select(StudentProfile.user_id).where(StudentProfile.class_id == hw.class_id)
        )
        target_student_ids = [row[0] for row in roster.all()]

    existing = await session.execute(
        select(StudentHomeworkProgress.student_id).where(
            StudentHomeworkProgress.homework_id == hw.id
        )
    )
    already_has_progress = {row[0] for row in existing.all()}
    for student_id in target_student_ids:
        if student_id not in already_has_progress:
            session.add(StudentHomeworkProgress(homework_id=hw.id, student_id=student_id))

    hw.assigned_count = len(set(target_student_ids) | already_has_progress)

    await session.commit()
    await session.refresh(hw)
    return await _serialize(session, hw)
