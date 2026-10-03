from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError

from src.api.deps import get_db_session, require_role
from src.api.schemas.bloom import BloomScore
from src.api.schemas.common import ListEnvelope
from src.api.schemas.test_result import ClassTestResultOut, StudentTestResultOut
from src.domain.models import (
    BloomLevel,
    Chapter,
    Role,
    StudentTestResult,
    StudentTestResultBloomScore,
    Topic,
    User,
    VoiceTest,
)
from src.repositories.lookups import get_test_for_teacher

router = APIRouter(prefix="/api/v1", tags=["test-results"])


async def _bloom_performance_for(session: AsyncSession, result_ids: list) -> list[BloomScore]:
    if not result_ids:
        return [BloomScore(level=level, percent=None) for level in BloomLevel]
    rows = await session.execute(
        select(StudentTestResultBloomScore.bloom_level, func.avg(StudentTestResultBloomScore.percent))
        .where(StudentTestResultBloomScore.student_test_result_id.in_(result_ids))
        .group_by(StudentTestResultBloomScore.bloom_level)
    )
    averages = {level: pct for level, pct in rows.all() if pct is not None}
    return [
        BloomScore(level=level, percent=round(averages[level]) if level in averages else None)
        for level in BloomLevel
    ]


async def _topic_label(session: AsyncSession, test: VoiceTest) -> str:
    if test.topic_id is not None:
        result = await session.execute(select(Topic.name).where(Topic.id == test.topic_id))
        name = result.scalar_one_or_none()
        if name:
            return name
    result = await session.execute(select(Chapter.name).where(Chapter.id == test.chapter_id))
    return result.scalar_one_or_none() or ""


@router.get("/tests/{test_id}/results/class")
async def get_class_result(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ClassTestResultOut:
    test = await get_test_for_teacher(session, test_id, user, ready=True)

    results = await session.execute(
        select(StudentTestResult).where(StudentTestResult.test_id == test.id)
    )
    student_results = results.scalars().all()
    result_ids = [r.id for r in student_results]

    class_mastery_percent = (
        round(sum(r.mastery_percent for r in student_results) / len(student_results))
        if student_results
        else 0
    )
    bloom_performance = await _bloom_performance_for(session, result_ids)
    assessed = [b.percent for b in bloom_performance if b.percent is not None]
    priority_gap_percent = min(assessed) if assessed else class_mastery_percent

    return ClassTestResultOut(
        test_id=str(test.id),
        class_mastery_percent=class_mastery_percent,
        assigned_count=test.assigned_count,
        completed_count=test.completed_count,
        priority_gap_topic=await _topic_label(session, test),
        priority_gap_percent=priority_gap_percent,
        bloom_performance=bloom_performance,
    )


@router.get("/tests/{test_id}/results/students")
async def list_student_results(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[StudentTestResultOut]:
    test = await get_test_for_teacher(session, test_id, user, ready=True)
    results = await session.execute(
        select(StudentTestResult).where(StudentTestResult.test_id == test.id)
    )
    student_results = results.scalars().all()
    items = [
        StudentTestResultOut(
            test_id=str(test.id),
            student_id=str(r.student_id),
            mastery_percent=r.mastery_percent,
            bloom_performance=await _bloom_performance_for(session, [r.id]),
        )
        for r in student_results
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/tests/{test_id}/results/students/{student_id}")
async def get_student_result(
    test_id: str,
    student_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> StudentTestResultOut:
    test = await get_test_for_teacher(session, test_id, user, ready=True)
    result = await session.execute(
        select(StudentTestResult).where(
            StudentTestResult.test_id == test.id, StudentTestResult.student_id == student_id
        )
    )
    student_result = result.scalar_one_or_none()
    if student_result is None:
        raise NotFoundError("Student didn't attempt this Test, or isn't in your class.")
    return StudentTestResultOut(
        test_id=str(test.id),
        student_id=student_id,
        mastery_percent=student_result.mastery_percent,
        bloom_performance=await _bloom_performance_for(session, [student_result.id]),
    )
