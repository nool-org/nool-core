from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.teacher_dashboard import (
    TeacherDashboardClassOption,
    TeacherDashboardOut,
    TeacherHeadlineTest,
)
from src.domain.models import Chapter, Role, SchoolClass, TeacherClassAssignment, TestStatus, User, VoiceTest
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

    headline_test = None
    if active is not None:
        test_result = await session.execute(
            select(VoiceTest)
            # only this teacher's subjects - the class's other teachers' tests aren't theirs to show
            .where(VoiceTest.class_id == active.id, VoiceTest.subject_id.in_(
                select(TeacherClassAssignment.subject_id).where(
                    TeacherClassAssignment.teacher_id == user.id, TeacherClassAssignment.class_id == active.id)))
            .order_by(VoiceTest.created_at.desc())
            .limit(1)
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
    )
