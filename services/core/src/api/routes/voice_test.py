import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.roster import AssignmentTarget
from src.api.schemas.voice_test import CreateTestIn, VoiceTestOut
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    Feature,
    Role,
    School,
    SchoolClass,
    StudentProfile,
    Subject,
    TeacherClassAssignment,
    TestStatus,
    Topic,
    User,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestTargetStudent,
)
from src.repositories import get_session
from src.services.subject_grades import ensure_subjects_enabled_for_school
from src.repositories.lookups import ensure_students_in_class, get_test_for_teacher
from src.repositories.roster_repository import teaches_class_subject
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import count_tests
from src.services import kg_client

logger = logging.getLogger(__name__)

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


async def _generate_reference_content(
    session: AsyncSession, test: VoiceTest, bloom_levels: list[str]
) -> None:
    """Grounds this Test's colearner session in real curriculum content -
    see kg's services/kg/src/api/routes/voice_test.py:generate. Deliberately
    swallows any failure (KG service down, no OPENAI_API_KEY there, chapter
    never synced from KG) rather than blocking test creation on it: a
    missing reference_questions/textbook_context just means the AI Assessor
    session falls back to colearner's own generic defaults (see
    services/colearner/src/services/chained_service.py's DEFAULT_CONTEXT).
    """
    chapter = await session.get(Chapter, test.chapter_id)
    if chapter is None or chapter.order_index is None:
        return  # Never synced from KG (see Chapter.order_index's docstring) - nothing to ground against.

    subject = await session.get(Subject, test.subject_id)
    school_class = await session.get(SchoolClass, test.class_id)
    school = await session.get(School, school_class.school_id) if school_class else None
    topic = await session.get(Topic, test.topic_id) if test.topic_id else None
    if subject is None or school_class is None or school is None:
        return

    try:
        result = await kg_client.generate_assessor_content(
            subject=subject.name,
            board=school.board,
            grade=school_class.grade,
            chapters=[{"number": chapter.order_index, "name": chapter.name}],
            topic_names=[topic.name] if topic else [],
            bloom_levels=bloom_levels,
            num_questions=max(len(bloom_levels), 2),
        )
    except Exception:
        logger.warning("voice_test_reference_content_generation_failed", extra={"test_id": str(test.id)})
        return

    test.reference_questions = result.get("reference_questions") or None
    test.textbook_context = result.get("textbook_context") or None
    await session.commit()


async def _generate_reference_content_in_background(test_id: uuid.UUID, bloom_levels: list[str]) -> None:
    """FastAPI BackgroundTasks entrypoint - runs after create_test's response
    has already been sent (see its `background_tasks.add_task(...)` call).

    A grounded generation call to kg (a real OpenAI call) reliably takes
    10-25s - previously awaited inline before create_test returned, which
    routinely blew past nool-apps' apiClient.ts's 10s REQUEST_TIMEOUT_MS,
    so the app showed test creation as failed/hung even though Core went on
    to create the test successfully a few seconds later. Opens its own
    session via get_session() rather than reusing create_test's - that one
    is closed by the time this runs, well after the response.
    """
    async with get_session() as session:
        test = await session.get(VoiceTest, test_id)
        if test is None:
            return  # Deleted/unreachable between request and background run - nothing to ground.
        await _generate_reference_content(session, test, bloom_levels)


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

    # Grounding the colearner session in real curriculum content is a real
    # LLM call (10-25s) - runs after this response is sent, not before, so
    # "Create" doesn't sit there for that long (see
    # _generate_reference_content_in_background's docstring). The test is
    # immediately usable either way; reference_questions/textbook_context
    # just fill in a few seconds later if generation succeeds.
    background_tasks.add_task(
        _generate_reference_content_in_background, test.id, [level.value for level in body.bloom_levels]
    )

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
