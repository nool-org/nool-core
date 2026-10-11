from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.bloom import BloomScore
from src.api.schemas.progress import (
    NextBestFocusOut,
    StudentProgressOverviewOut,
    SubjectProgressSummaryOut,
    SubjectTestSummaryOut,
    TrajectoryPointOut,
)
from src.domain.models import (
    BloomLevel,
    Chapter,
    Role,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    User,
    VoiceTest,
)

router = APIRouter(prefix="/api/v1", tags=["progress"])


@router.get("/me/progress")
async def get_my_progress(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> StudentProgressOverviewOut:
    result_ids_rows = await session.execute(
        select(StudentTestResult.id).where(StudentTestResult.student_id == user.id)
    )
    result_ids = [row[0] for row in result_ids_rows.all()]

    bloom_mastery: list[BloomScore]
    if result_ids:
        averages_rows = await session.execute(
            select(StudentTestResultBloomScore.bloom_level, func.avg(StudentTestResultBloomScore.percent))
            .where(StudentTestResultBloomScore.student_test_result_id.in_(result_ids))
            .group_by(StudentTestResultBloomScore.bloom_level)
        )
        averages = {level: pct for level, pct in averages_rows.all() if pct is not None}
        bloom_mastery = [
            BloomScore(level=level, percent=round(averages[level]) if level in averages else None)
            for level in BloomLevel
        ]
    else:
        bloom_mastery = [BloomScore(level=level, percent=None) for level in BloomLevel]

    subject_rows = await session.execute(
        select(Subject, func.avg(StudentTestResult.mastery_percent), func.count(StudentTestResult.id))
        .join(VoiceTest, VoiceTest.subject_id == Subject.id)
        .join(StudentTestResult, StudentTestResult.test_id == VoiceTest.id)
        .where(StudentTestResult.student_id == user.id)
        .group_by(Subject.id)
    )
    subject_summaries = [
        SubjectProgressSummaryOut(
            subject_id=str(subject.id),
            subject_label=subject.name,
            tests_count=count,
            concepts_tracked=0,
            score_percent=round(avg_pct or 0),
            score_trend_label="",
            strongest_concept="",
            strongest_concept_percent=0,
            focus_concept="",
            focus_concept_percent=0,
        )
        for subject, avg_pct, count in subject_rows.all()
    ]

    # Weekly mastery trend for this one student - same week_expr/date_trunc
    # pattern school_analytics.py's compute_school_analytics already uses
    # for the school-wide trend, just scoped to student_id instead of
    # school_id. Previously this was a single fake "Now" point (the
    # average of bloom_mastery, itself always empty before
    # record_test_completion existed) - now a real multi-week series once
    # more than one week of results exists.
    week_expr = func.date_trunc("week", VoiceTest.created_at)
    trend_rows = await session.execute(
        select(week_expr, func.avg(StudentTestResult.mastery_percent))
        .select_from(StudentTestResult)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .where(StudentTestResult.student_id == user.id)
        .group_by(week_expr)
        .order_by(week_expr)
    )
    trend = trend_rows.all()[-8:]
    if trend:
        trajectory = [
            TrajectoryPointOut(label=week_start.strftime("%b %-d"), mastery_percent=round(avg_pct))
            for week_start, avg_pct in trend
        ]
        gain = trajectory[-1].mastery_percent - trajectory[0].mastery_percent
        trajectory_gain_label = (
            "No change yet" if len(trajectory) < 2 else (f"+{gain} pts" if gain >= 0 else f"{gain} pts")
        )
    else:
        trajectory = [TrajectoryPointOut(label="Now", mastery_percent=0)]
        trajectory_gain_label = "Not enough tests yet"

    # The Bloom level this student is weakest at, among levels that have
    # actually been assessed - a real "what to work on next" signal
    # instead of the always-empty placeholder this used to be.
    assessed = [b for b in bloom_mastery if b.percent is not None]
    if assessed:
        weakest = min(assessed, key=lambda b: b.percent or 0)
        next_best_focus = NextBestFocusOut(
            label=f"{weakest.level.value.title()}-level questions",
            message=f"Your {weakest.level.value.lower()}-level mastery is {weakest.percent}% - the lowest of your assessed levels. A few more questions at this level would help most.",
        )
    else:
        next_best_focus = NextBestFocusOut(
            label="Take your first test",
            message="Once you complete a Voice Test, your Bloom-level breakdown will show up here.",
        )

    # Each finished voice test, newest first - chapter name and score.
    finished = await session.execute(
        select(StudentTestResult.test_id, StudentTestResult.mastery_percent, Subject.id, Subject.name, Chapter.name)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .join(Subject, Subject.id == VoiceTest.subject_id)
        .outerjoin(Chapter, Chapter.id == VoiceTest.chapter_id)
        .where(StudentTestResult.student_id == user.id)
        .order_by(VoiceTest.created_at.desc())
        .limit(20)
    )
    subject_tests = [
        SubjectTestSummaryOut(
            subject_id=str(subject_id), label=chapter_name or subject_name, test_type=f"Voice test · {subject_name}",
            score_percent=mastery, trend_label="", test_id=str(test_id),
        )
        for test_id, mastery, subject_id, subject_name, chapter_name in finished.all()
    ]

    return StudentProgressOverviewOut(
        bloom_mastery=bloom_mastery,
        subject_summaries=subject_summaries,
        subject_tests=subject_tests,
        trajectory=trajectory,
        trajectory_gain_label=trajectory_gain_label,
        next_best_focus=next_best_focus,
    )
