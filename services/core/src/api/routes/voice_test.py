import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.roster import AssignmentTarget
from src.api.schemas.voice_test import (
    CreateTestIn,
    SessionReviewListOut,
    SessionReviewOut,
    UpdateVoiceTestQuestionIn,
    VoiceTestOut,
    VoiceTestQuestionListOut,
    VoiceTestQuestionOut,
)
from src.domain.models.voice_test import VoiceTestPrepStatus
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    AssignmentTargetMode,
    Chapter,
    Feature,
    Role,
    SchoolClass,
    StudentProfile,
    TeacherClassAssignment,
    TestStatus,
    Topic,
    User,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestQuestion,
    VoiceTestTargetStudent,
)
from src.services.subject_grades import ensure_subjects_enabled_for_school
from src.repositories.lookups import ensure_students_in_class, get_test_for_teacher
from src.repositories.roster_repository import teaches_class_subject
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import count_tests
from src.services import voice_test_prep

router = APIRouter(prefix="/api/v1", tags=["voice-tests"])


async def _serialize(session: AsyncSession, test: VoiceTest) -> VoiceTestOut:
    bloom_result = await session.execute(
        select(VoiceTestBloomLevel.bloom_level).where(VoiceTestBloomLevel.test_id == test.id)
    )
    bloom_levels = [row[0] for row in bloom_result.all()]

    student_ids: list[str] | None = None
    if test.target_mode == AssignmentTargetMode.SPECIFIC_STUDENTS:
        targets = await session.execute(
            select(VoiceTestTargetStudent.student_id).where(VoiceTestTargetStudent.test_id == test.id)
        )
        student_ids = [str(row[0]) for row in targets.all()]

    return VoiceTestOut(
        id=str(test.id),
        class_id=str(test.class_id),
        subject_id=str(test.subject_id),
        chapter_id=str(test.chapter_id),
        topic_id=str(test.topic_id) if test.topic_id else None,
        bloom_levels=bloom_levels,
        duration_minutes=test.duration_minutes,
        completion_window_hours=test.completion_window_hours,
        target=AssignmentTarget(mode=test.target_mode, student_ids=student_ids),
        status=test.status,
        assigned_count=test.assigned_count,
        completed_count=test.completed_count,
        created_at=test.created_at,
    )


def _question_out(question: VoiceTestQuestion) -> VoiceTestQuestionOut:
    return VoiceTestQuestionOut(
        id=str(question.id),
        voice_test_id=str(question.voice_test_id),
        order=question.order,
        dataset_question_id=str(question.dataset_question_id) if question.dataset_question_id else None,
        bloom_level=question.bloom_level,
        topic_id=str(question.topic_id) if question.topic_id else None,
        subtopic_id=str(question.subtopic_id) if question.subtopic_id else None,
        concepts=question.concepts or [],
        original_text=question.original_text,
        reworded_text=question.reworded_text,
        spoken_text=question.spoken_text,
        expected_points=question.expected_points or [],
        misconceptions=question.misconceptions or [],
        hints=question.hints or [],
        status=question.status,
    )


async def _editable_question(session: AsyncSession, test_id: str, question_id: str, user: User) -> tuple[VoiceTest, VoiceTestQuestion]:
    test = await get_test_for_teacher(session, test_id, user)
    try:
        question = await session.get(VoiceTestQuestion, uuid.UUID(question_id))
    except ValueError:
        question = None
    if question is None or question.voice_test_id != test.id:
        raise NotFoundError("Question not found.")
    if test.completed_count:
        # every student must be asked the same questions
        raise ConflictError("Students have already taken this test - its questions can't change now.")
    return test, question


@router.get("/tests")
async def list_tests(
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[VoiceTestOut]:
    result = await session.execute(
        select(VoiceTest)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        # only tests of classes + subjects this teacher teaches
        .join(TeacherClassAssignment, and_(
            TeacherClassAssignment.class_id == VoiceTest.class_id,
            TeacherClassAssignment.subject_id == VoiceTest.subject_id,
            TeacherClassAssignment.teacher_id == user.id))
        .where(SchoolClass.school_id == user.school_id)
    )
    tests = result.scalars().all()
    items = [await _serialize(session, t) for t in tests]
    return ListEnvelope(items=items, total=len(items))


@router.get("/tests/{test_id}")
async def get_test(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceTestOut:
    test = await get_test_for_teacher(session, test_id, user)
    return await _serialize(session, test)


@router.post("/tests", status_code=201, summary="Create a Test (DRAFT)")
async def create_test(
    body: CreateTestIn,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceTestOut:
    if not await teaches_class_subject(session, user.id, body.class_id, body.subject_id):
        raise ForbiddenError("You don't teach that subject in that class.")
    await ensure_subjects_enabled_for_school(session, [body.subject_id], user.school_id)
    chapter = await session.get(Chapter, body.chapter_id)
    if chapter is None or str(chapter.subject_id) != str(body.subject_id):
        raise ValidationError("That chapter isn't part of this subject.")
    if body.topic_id:
        topic = await session.get(Topic, body.topic_id)
        if topic is None or str(topic.chapter_id) != str(body.chapter_id):
            raise ValidationError("That topic isn't part of this chapter.")
    if body.target.mode == AssignmentTargetMode.SPECIFIC_STUDENTS and not body.target.student_ids:
        raise ValidationError("target.studentIds must be non-empty when mode is SPECIFIC_STUDENTS.")

    plan = await get_active_plan(session, user.school_id)
    if plan is not None and plan.test_limit is not None:
        if await count_tests(session, user.school_id) >= plan.test_limit:
            raise ConflictError(f"Your plan allows up to {plan.test_limit} Voice Tests.")

    test = VoiceTest(
        class_id=body.class_id,
        subject_id=body.subject_id,
        chapter_id=body.chapter_id,
        topic_id=body.topic_id,
        duration_minutes=body.duration_minutes,
        completion_window_hours=body.completion_window_hours,
        target_mode=body.target.mode,
        status=TestStatus.DRAFT,
        # question prep runs in the background right after this; sessions wait until it settles
        prep_status=VoiceTestPrepStatus.PENDING,
    )
    session.add(test)
    await session.flush()

    for level in body.bloom_levels:
        session.add(VoiceTestBloomLevel(test_id=test.id, bloom_level=level))
    if body.target.mode == AssignmentTargetMode.SPECIFIC_STUDENTS:
        # only students of the test's class - never another class/school
        target_student_ids = [str(s) for s in await ensure_students_in_class(
            session, body.target.student_ids or [], body.class_id)]
        if not target_student_ids:
            raise ValidationError("Pick at least one student.")
    else:
        # WHOLE_CLASS previously left VoiceTestTargetStudent completely
        # empty - every read path a student's own app uses to see "my
        # assigned tests" (GET /me/assigned-tests, /me/assigned-tests/
        # {id}, and the dashboard's pending_tests query) joins through
        # this exact table, which its own docstring used to claim was
        # "only populated when target_mode is SPECIFIC_STUDENTS" - so a
        # WHOLE_CLASS test reached zero students in practice, not just
        # some. Resolving the class roster here, once, at creation time
        # makes both modes go through the identical downstream delivery
        # path - no separate "is this WHOLE_CLASS, go look up the class
        # roster instead" branch needed anywhere else in the codebase.
        roster = await session.execute(
            select(StudentProfile.user_id).where(StudentProfile.class_id == body.class_id)
        )
        target_student_ids = [str(row[0]) for row in roster.all()]
    for student_id in target_student_ids:
        session.add(VoiceTestTargetStudent(test_id=test.id, student_id=student_id))
    test.assigned_count = len(target_student_ids)

    await session.commit()
    await session.refresh(test)

    # Picking + rewording the questions are LLM calls (10-60s) - run after
    # this response is sent so "Create" stays fast (the app's client times
    # out at 10s). The questions show up in GET /tests/{id}/questions once done.
    background_tasks.add_task(voice_test_prep.prepare_in_background, test.id, user.id)

    return await _serialize(session, test)


@router.post("/tests/{test_id}/schedule", summary="Make the Test live")
async def schedule_test(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceTestOut:
    test = await get_test_for_teacher(session, test_id, user)
    if test.status != TestStatus.DRAFT:
        raise ConflictError("Test isn't in a schedulable state.")
    test.status = TestStatus.SCHEDULED
    await session.commit()
    await session.refresh(test)
    return await _serialize(session, test)


@router.get("/tests/{test_id}/questions", summary="The questions every student of the test is asked")
async def list_test_questions(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceTestQuestionListOut:
    test = await get_test_for_teacher(session, test_id, user)
    questions = (await session.execute(
        select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test.id).order_by(VoiceTestQuestion.order)
    )).scalars().all()
    return VoiceTestQuestionListOut(items=[_question_out(q) for q in questions])


@router.patch("/tests/{test_id}/questions/{question_id}", summary="Edit or approve a question")
async def update_test_question(
    test_id: str,
    question_id: str,
    body: UpdateVoiceTestQuestionIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceTestQuestionOut:
    _test, question = await _editable_question(session, test_id, question_id, user)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(question, field, value.strip() if isinstance(value, str) else value)
    await session.commit()
    return _question_out(question)


@router.post("/tests/{test_id}/questions/{question_id}/swap", summary="Replace a question with another of its level")
async def swap_test_question(
    test_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceTestQuestionOut:
    test, question = await _editable_question(session, test_id, question_id, user)
    if not await voice_test_prep.swap_question(session, test, question, user):
        raise ConflictError("No other question of this Bloom level is available for this chapter.")
    await session.commit()
    return _question_out(question)


@router.get("/tests/{test_id}/reviews", summary="Sessions whose grading failed - for the teacher to review")
async def list_test_reviews(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> SessionReviewListOut:
    test = await get_test_for_teacher(session, test_id, user)
    rows = (await session.execute(
        select(AiAssessorSessionRecord, AiAssessorSession.student_id, User.display_name)
        .join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .join(User, User.id == AiAssessorSession.student_id)
        .where(AiAssessorSession.test_id == test.id, AiAssessorSessionRecord.needs_review.is_(True))
        .order_by(AiAssessorSessionRecord.created_at)
    )).all()
    return SessionReviewListOut(items=[SessionReviewOut(
        session_id=str(record.session_id),
        student_id=str(student_id),
        student_name=name or "",
        finished_at=record.created_at,
        grading_error=str((record.report or {}).get("grading_error") or ""),
        transcript=record.transcript or [],
        answers=record.answers or [],
    ) for record, student_id, name in rows])
