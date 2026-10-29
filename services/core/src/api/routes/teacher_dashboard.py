from datetime import datetime, timedelta, timezone
from statistics import mean

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.teacher_dashboard import (
    TeacherClassStats,
    TeacherDashboardClassOption,
    TeacherDashboardOut,
    TeacherHeadlineTest,
    WeakestTopicOut,
)
from src.domain.models import (
    Chapter, Role, SchoolClass, StudentTestResult, StudentTestResultTopicScore, TeacherClassAssignment, TestStatus,
    Topic, User, VoiceTest,
)
from src.services.daily_practice import tidy
from src.repositories.roster_repository import student_count

router = APIRouter(prefix="/api/v1", tags=["teacher-dashboard"])


async def _class_option(session: AsyncSession, school_class: SchoolClass) -> TeacherDashboardClassOption:
    count = await student_count(session, school_class.id)
    return TeacherDashboardClassOption(
        id=str(school_class.id),
        label=f"Class {school_class.grade} · {school_class.section}",
        meta=f"{count} student{'' if count == 1 else 's'}",
    )


def _window_status(test: VoiceTest) -> TestStatus:
    """A sent Test stays SCHEDULED for its whole window; read live / closed from
    the window (same rule as nool-apps' ApiTestService.withWindowStatus)."""
    if test.status not in (TestStatus.SCHEDULED, TestStatus.ACTIVE):
        return test.status
    now = datetime.now(timezone.utc)
    if test.due_at is not None and test.due_at <= now:
        return TestStatus.COMPLETED
    if test.scheduled_at is not None and test.scheduled_at > now:
        return TestStatus.SCHEDULED
    return TestStatus.ACTIVE


NEED_HELP_BELOW = 50  # latest test mastery under this = needs help


async def _class_stats(session: AsyncSession, tests) -> tuple[TeacherClassStats | None, WeakestTopicOut | None]:
    """The class at a glance over `tests` (a select of this teacher's test ids in the class)."""
    rows = (await session.execute(
        select(StudentTestResult.student_id, StudentTestResult.mastery_percent, StudentTestResult.created_at)
        .where(StudentTestResult.test_id.in_(tests)).order_by(StudentTestResult.created_at))).all()
    if not rows:
        return None, None
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    latest = {student: mastery for student, mastery, _ in rows}
    then = {student: mastery for student, mastery, at in rows if at <= week_ago}
    average = round(mean(latest.values()))
    stats = TeacherClassStats(average_percent=average, week_delta=average - round(mean(then.values())) if then else None,
                              need_help=sum(m < NEED_HELP_BELOW for m in latest.values()))

    scores = (await session.execute(
        select(StudentTestResultTopicScore.topic_id, StudentTestResultTopicScore.percent, StudentTestResult.test_id)
        .join(StudentTestResult, StudentTestResult.id == StudentTestResultTopicScore.student_test_result_id)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .where(StudentTestResult.test_id.in_(tests), StudentTestResultTopicScore.answered > 0)
        .order_by(VoiceTest.created_at))).all()
    by_topic: dict = {}
    for topic_id, percent, test_id in scores:
        by_topic.setdefault(topic_id, {}).setdefault(test_id, []).append(percent)
    if not by_topic:
        return stats, None
    overall = {t: mean(p for per_test in tests_.values() for p in per_test) for t, tests_ in by_topic.items()}
    weakest = min(overall, key=overall.get)
    topic = await session.get(Topic, weakest)
    return stats, WeakestTopicOut(name=tidy(topic.name), percent=round(overall[weakest]),
                                  trend=[round(mean(p)) for p in list(by_topic[weakest].values())[-5:]])


@router.get("/teacher/dashboard")
async def get_teacher_dashboard(
    class_id: str | None = None,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> TeacherDashboardOut:
    assigned = await session.execute(
        select(SchoolClass)
        .join(TeacherClassAssignment, TeacherClassAssignment.class_id == SchoolClass.id)
        .where(TeacherClassAssignment.teacher_id == user.id, SchoolClass.school_id == user.school_id)
        .distinct()
    )
    classes = assigned.scalars().all()
    class_options = [await _class_option(session, c) for c in classes]

    active = next((c for c in classes if str(c.id) == class_id), classes[0] if classes else None)
    active_option = next((o for o in class_options if o.id == str(active.id)), None) if active else None

    headline_test, stats, weakest_topic = None, None, None
    if active is not None:
        # only this teacher's subjects - the class's other teachers' tests aren't theirs to show
        mine = (VoiceTest.class_id == active.id, VoiceTest.subject_id.in_(
            select(TeacherClassAssignment.subject_id).where(
                TeacherClassAssignment.teacher_id == user.id, TeacherClassAssignment.class_id == active.id)))
        stats, weakest_topic = await _class_stats(session, select(VoiceTest.id).where(*mine))
        test_result = await session.execute(
            # a draft is only the headline when there is nothing else (T-025)
            select(VoiceTest).where(*mine).order_by((VoiceTest.status == TestStatus.DRAFT), VoiceTest.created_at.desc()).limit(1)
        )
        test = test_result.scalar_one_or_none()
        if test is not None:
            chapter = await session.get(Chapter, test.chapter_id)
            status = _window_status(test)
            label = {TestStatus.ACTIVE: "Live", TestStatus.COMPLETED: "Closed"}.get(
                status, status.value.replace('_', ' ').capitalize())
            headline_test = TeacherHeadlineTest(
                id=str(test.id),
                title=chapter.name if chapter else "Chat with Milo",
                meta=f"{label} · {test.completed_count}/{test.assigned_count} complete",
                status=status,
            )

    return TeacherDashboardOut(
        class_options=class_options,
        active_class=active_option or TeacherDashboardClassOption(id="", label="", meta="0 students"),
        headline_test=headline_test,
        stats=stats,
        weakest_topic=weakest_topic,
    )
