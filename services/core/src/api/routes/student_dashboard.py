from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.routes.assigned_test import taken_test_ids
from src.api.schemas.student_dashboard import (
    ContinueJourneyOut,
    LearningStreakOut,
    PendingActivityKind,
    ProgressSummaryOut,
    StreakDayOut,
    StreakDayState,
    StudentDashboardOut,
    SubjectTodayOut,
)
from src.domain.models import (
    Chapter,
    Role,
    StudentHomeworkProgress,
    StudentTestResult,
    Subject,
    TestStatus,
    User,
    VoiceTest,
    VoiceTestTargetStudent,
)

router = APIRouter(prefix="/api/v1", tags=["student-dashboard"])

_DAY_LABELS = ["M", "T", "W", "T", "F", "S", "S"]


async def _learning_streak(session: AsyncSession, student_id) -> LearningStreakOut:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    confirmed = await session.execute(
        select(StudentHomeworkProgress.confirmed_at).where(
            StudentHomeworkProgress.student_id == student_id,
            StudentHomeworkProgress.confirmed_at.is_not(None),
            StudentHomeworkProgress.confirmed_at >= since,
        )
    )
    active_dates = {row[0].date() for row in confirmed.all()}
    today = datetime.now(timezone.utc).date()

    days = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        if day == today:
            state = StreakDayState.TODAY
        elif day in active_dates:
            state = StreakDayState.DONE
        else:
            state = StreakDayState.UPCOMING
        days.append(StreakDayOut(label=_DAY_LABELS[day.weekday()], state=state))

    return LearningStreakOut(
        current_streak_days=len(active_dates),
        days=days,
        message="Complete one meaningful learning activity today to continue your streak.",
    )


def _time_left(due_at: datetime | None) -> str | None:
    """"5 h left" / "2 days left" / "40 min left" - the student-facing deadline."""
    if due_at is None:
        return None
    minutes = int((due_at - datetime.now(timezone.utc)).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min left"
    hours = minutes // 60
    return f"{hours} h left" if hours < 48 else f"{hours // 24} days left"


@router.get("/me/dashboard")
async def get_student_dashboard(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> StudentDashboardOut:
    streak = await _learning_streak(session, user.id)

    # Excludes a Test the student has already completed - see
    # assigned_test.py's list_my_tests for the same fix and the same
    # reasoning (a finished Test used to never leave this "pending" list).
    pending_tests = await session.execute(
        select(VoiceTest, Subject)
        .join(VoiceTestTargetStudent, VoiceTestTargetStudent.test_id == VoiceTest.id)
        .join(Subject, Subject.id == VoiceTest.subject_id)
        .where(
            VoiceTestTargetStudent.student_id == user.id,
            VoiceTest.id.not_in(taken_test_ids(user.id)),
            VoiceTest.status != TestStatus.DRAFT,  # same rule as assigned_test.py
            VoiceTest.still_open(),
        )
        .order_by(VoiceTest.created_at)
    )
    # One "today" entry per subject (the app keys its subject chips by
    # subject id - two pending tests in one subject made duplicate chips).
    first_per_subject: dict = {}
    for test, subject in pending_tests.all():
        first_per_subject.setdefault(subject.id, (test, subject))
    chapter_names = dict((await session.execute(
        select(Chapter.id, Chapter.name).where(Chapter.id.in_([t.chapter_id for t, _ in first_per_subject.values()]))
    )).all()) if first_per_subject else {}
    subject_today = [
        SubjectTodayOut(
            subject_id=str(subject.id),
            subject_label=subject.name,
            kind=PendingActivityKind.TEST,
            title=chapter_names.get(test.chapter_id) or subject.name,
            meta=" · ".join(filter(None, ["Voice test", _time_left(test.due_at)])),
            progress_percent=0,
            for_you_title=f"Continue {subject.name}",
            for_you_meta="Recommended from your last session",
        )
        for test, subject in list(first_per_subject.values())[:3]
    ]

    results = await session.execute(
        select(StudentTestResult.mastery_percent).where(StudentTestResult.student_id == user.id)
    )
    scores = [row[0] for row in results.all()]
    overall = round(sum(scores) / len(scores)) if scores else 0

    return StudentDashboardOut(
        streak=streak,
        continue_journey=(
            ContinueJourneyOut(
                subject_id=subject_today[0].subject_id,
                title=subject_today[0].title,
                message="Your Co-learner is ready to continue your session.",
            )
            if subject_today
            else None
        ),
        subject_today=subject_today,
        progress_summary=ProgressSummaryOut(
            overall_mastery_percent=overall, mastery_trend_label="", topics_improving=0
        ),
    )
