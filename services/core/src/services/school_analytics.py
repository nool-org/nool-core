"""Shared analytics computation for one school - extracted from
school_admin.py's GET /school/analytics so a Super Admin can compute the
exact same, real, non-snapshotted numbers for an explicit school_id (see
requirements.md's usage-insight hierarchy: Platform -> School -> Class).
Every number here is computed live from real rows at read time - no
snapshot table, so it's always exactly consistent, same guarantee
MasteryTrendPointOut's docstring already makes.
"""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.bloom import BloomScore
from src.api.schemas.school_admin import (
    ClassBreakdownOut,
    MasteryTrendPointOut,
    SchoolAnalyticsOut,
    SlippingStudentOut,
    WeakChapterOut,
)
from src.domain.models import (
    BloomLevel,
    Chapter,
    DailyPracticeSession,
    QuizTest,
    SchoolClass,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    User,
    VoiceTest,
)
from src.services.daily_practice import tidy

SLIP_POINTS = 15  # latest challenge this far under the student's own earlier average
VERY_LOW = 40


async def _challenge_results(session: AsyncSession, school_id) -> list[tuple]:
    """Every scored challenge attempt at this school, both kinds - Speak with Milo
    results and first Fix Milo's homework attempts - as
    (challenge_id, class_id, chapter_id, student_id, percent, when), oldest first.
    ponytail: whole school in memory (~30k rows for 1k students x 30 challenges);
    aggregate in SQL if schools get far bigger."""
    voice = (await session.execute(
        select(VoiceTest.id, VoiceTest.class_id, VoiceTest.chapter_id, StudentTestResult.student_id,
               StudentTestResult.mastery_percent, VoiceTest.created_at)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .where(SchoolClass.school_id == school_id))).all()
    fix = (await session.execute(
        select(QuizTest.id, QuizTest.class_id, QuizTest.chapter_id, DailyPracticeSession.student_id,
               DailyPracticeSession.score_percent, DailyPracticeSession.completed_at)
        .join(QuizTest, QuizTest.id == DailyPracticeSession.quiz_test_id)
        .join(SchoolClass, SchoolClass.id == QuizTest.class_id)
        .where(SchoolClass.school_id == school_id, DailyPracticeSession.attempt == 1,
               DailyPracticeSession.score_percent.is_not(None), DailyPracticeSession.completed_at.is_not(None)))).all()
    return sorted([tuple(r) for r in (*voice, *fix)], key=lambda r: r[5])


def _avg(values) -> int:
    values = list(values)
    return round(sum(values) / len(values)) if values else 0


async def compute_school_analytics(session: AsyncSession, school_id: UUID | str) -> SchoolAnalyticsOut:
    classes_result = await session.execute(select(SchoolClass).where(SchoolClass.school_id == school_id))
    classes = classes_result.scalars().all()

    school_mastery_row = await session.execute(
        select(func.avg(StudentTestResult.mastery_percent))
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .where(SchoolClass.school_id == school_id)
    )
    school_mastery_avg = school_mastery_row.scalar_one_or_none()

    results = await _challenge_results(session, school_id)
    by_class: dict = defaultdict(list)
    for row in results:
        by_class[row[1]].append(row)
    labels = {c.id: f"Class {c.grade} · {c.section}" for c in classes}

    class_breakdown = []
    for c in classes:
        rows = by_class.get(c.id, [])
        per_challenge: dict = defaultdict(list)  # in the order they were taken
        for challenge_id, *_rest, percent, _when in rows:
            per_challenge[challenge_id].append(percent)
        averages = [_avg(v) for v in per_challenge.values()]
        class_breakdown.append(
            ClassBreakdownOut(
                class_id=str(c.id), label=labels[c.id],
                mastery_avg_percent=_avg(r[4] for r in rows),
                improvement_percent=averages[-1] - averages[0] if len(averages) >= 2 else 0,
                challenges_taken=len(per_challenge),
                students_assessed=len({r[3] for r in rows}),
            )
        )

    bloom_averages = []
    for level in BloomLevel:
        bloom_row = await session.execute(
            select(func.avg(StudentTestResultBloomScore.percent))
            .join(
                StudentTestResult,
                StudentTestResult.id == StudentTestResultBloomScore.student_test_result_id,
            )
            .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
            .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
            .where(SchoolClass.school_id == school_id, StudentTestResultBloomScore.bloom_level == level)
        )
        percent = bloom_row.scalar_one_or_none()
        bloom_averages.append(BloomScore(level=level, percent=round(percent) if percent is not None else None))

    week_expr = func.date_trunc("week", VoiceTest.created_at)
    trend_rows = await session.execute(
        select(week_expr, func.avg(StudentTestResult.mastery_percent), func.count(StudentTestResult.id))
        .select_from(StudentTestResult)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .where(SchoolClass.school_id == school_id)
        .group_by(week_expr)
        .order_by(week_expr)
    )
    mastery_trend = [
        MasteryTrendPointOut(
            period_label=week_start.strftime("%b %-d"),
            mastery_avg_percent=round(avg_mastery) if avg_mastery is not None else 0,
            test_count=count,
        )
        for week_start, avg_mastery, count in trend_rows.all()[-8:]
    ]

    weak_chapters = await _weak_chapters(session, results, labels)
    slipping = await _slipping(session, results, labels)

    return SchoolAnalyticsOut(
        school_mastery_avg_percent=round(school_mastery_avg) if school_mastery_avg is not None else 0,
        class_breakdown=class_breakdown,
        bloom_averages=bloom_averages,
        mastery_trend=mastery_trend,
        weak_chapters=weak_chapters,
        slipping_students=slipping,
    )


async def _weak_chapters(session: AsyncSession, results, labels) -> list[WeakChapterOut]:
    """The 8 chapters with the lowest average across the school - where to put teaching time."""
    by_chapter: dict = defaultdict(list)
    for _cid, class_id, chapter_id, student_id, percent, _when in results:
        by_chapter[chapter_id].append((class_id, student_id, percent))
    if not by_chapter:
        return []
    names = {ch_id: (ch_name, subj) for ch_id, ch_name, subj in (await session.execute(
        select(Chapter.id, Chapter.name, Subject.name).join(Subject, Subject.id == Chapter.subject_id)
        .where(Chapter.id.in_(by_chapter)))).all()}
    rows = [
        WeakChapterOut(
            chapter_id=str(ch), chapter=tidy(names.get(ch, ("", ""))[0]), subject=names.get(ch, ("", ""))[1],
            average_percent=_avg(p for *_x, p in got), students=len({s for _c, s, _p in got}),
            classes=sorted({labels[c] for c, _s, _p in got if c in labels}),
        )
        for ch, got in by_chapter.items()
    ]
    return sorted(rows, key=lambda r: r.average_percent)[:8]


async def _slipping(session: AsyncSession, results, labels) -> list[SlippingStudentOut]:
    """Students whose latest challenge is SLIP_POINTS under their own earlier
    average, or under VERY_LOW - biggest drop first, at most 20."""
    by_student: dict = defaultdict(list)
    for row in results:
        by_student[row[3]].append(row)
    found = []
    for student_id, rows in by_student.items():
        *earlier, latest = rows
        before = _avg(r[4] for r in earlier) if earlier else None
        drop = (before - latest[4]) if before is not None else 0
        if drop >= SLIP_POINTS or latest[4] < VERY_LOW:
            found.append((drop, student_id, latest, before))
    if not found:
        return []
    found.sort(key=lambda f: (-f[0], f[2][4]))
    found = found[:20]
    ids = [f[1] for f in found] + [f[2][2] for f in found]
    names = dict((await session.execute(select(User.id, User.display_name).where(User.id.in_(ids)))).all())
    chapters = dict((await session.execute(select(Chapter.id, Chapter.name).where(Chapter.id.in_(ids)))).all())
    return [
        SlippingStudentOut(
            student_id=str(sid), name=names.get(sid, "Student"), class_label=labels.get(latest[1], ""),
            chapter=tidy(chapters.get(latest[2], "")), latest_percent=latest[4], earlier_avg_percent=before,
        )
        for _drop, sid, latest, before in found
    ]
