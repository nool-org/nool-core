from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError

from src.api.deps import get_db_session, require_role
from src.api.schemas.assigned_test import AssignedTestOut
from src.api.schemas.common import ListEnvelope
from src.domain.models import (
    Role,
    StudentTestResult,
    TestStatus,
    Subject,
    User,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestTargetStudent,
)

router = APIRouter(prefix="/api/v1", tags=["assigned-tests"])


async def _serialize(session: AsyncSession, test: VoiceTest) -> AssignedTestOut:
    subject_result = await session.execute(select(Subject).where(Subject.id == test.subject_id))
    subject = subject_result.scalar_one_or_none()
    bloom_result = await session.execute(
        select(VoiceTestBloomLevel.bloom_level).where(VoiceTestBloomLevel.test_id == test.id)
    )
    return AssignedTestOut(
        id=str(test.id),
        subject_id=str(test.subject_id),
        subject_label=subject.name if subject else "",
        chapter_id=str(test.chapter_id),
        title=subject.name if subject else "Test",
        meta=f"{test.duration_minutes} min",
        in_progress=False,
        bloom_levels=[row[0] for row in bloom_result.all()],
        duration_minutes=test.duration_minutes,
    )


@router.get("/me/assigned-tests")
async def list_my_tests(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[AssignedTestOut]:
    """Returns items: [] when nothing is assigned yet - a real empty state.

    Excludes a Test the student has already completed (a real
    StudentTestResult row exists for it) - before this, a finished Test
    never left this "current" list, since the only filter here was class
    membership, not the student's own completion. GET /me/assigned-tests/
    {test_id} below stays unfiltered - a student who navigates straight to
    a finished Test (e.g. from its Results) can still open it.
    """
    already_done = select(StudentTestResult.test_id).where(StudentTestResult.student_id == user.id)
    result = await session.execute(
        select(VoiceTest)
        .join(VoiceTestTargetStudent, VoiceTestTargetStudent.test_id == VoiceTest.id)
        .where(
            VoiceTestTargetStudent.student_id == user.id,
            VoiceTest.id.not_in(already_done),
            VoiceTest.status != TestStatus.DRAFT,  # a teacher's unpublished draft
        )
    )
    items = [await _serialize(session, t) for t in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/me/assigned-tests/{test_id}")
async def get_my_test(
    test_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> AssignedTestOut:
    result = await session.execute(
        select(VoiceTest)
        .join(VoiceTestTargetStudent, VoiceTestTargetStudent.test_id == VoiceTest.id)
        .where(
            VoiceTest.id == test_id,
            VoiceTestTargetStudent.student_id == user.id,
            VoiceTest.status != TestStatus.DRAFT,
        )
    )
    test = result.scalar_one_or_none()
    if test is None:
        raise NotFoundError("Not assigned to you.")
    return await _serialize(session, test)
