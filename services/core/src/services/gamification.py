"""Bloom badges, class quests and the homework boss-question pick - the
Wave 3 additions to GET /me/gamification (src/api/routes/gamification.py).
"""

import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    AiAssessorSession,
    BloomLevel,
    Homework,
    StudentHomeworkProgress,
    StudentProfile,
    StudentTestResult,
    StudentTestResultBloomScore,
    TestStatus,
    VoiceTest,
)
from src.domain.models.gamification import StudentBadge

BADGE_THRESHOLD = 80
# (id, name, level) - fixed in-code catalog, like DAILY_GOAL_CATALOG.
BADGES = [
    ("badge-explainer", "Explainer", BloomLevel.UNDERSTAND),
    ("badge-problem-solver", "Problem Solver", BloomLevel.APPLY),
    ("badge-detective", "Detective", BloomLevel.ANALYZE),
    ("badge-judge", "Judge", BloomLevel.EVALUATE),
]

VOICE_TEST_QUEST_SHARE = 0.8
HOMEWORK_QUEST_TARGET = 50
# ponytail: quest weeks are fixed to IST (every school is in India); a
# per-school timezone column if that ever stops being true.
IST = timezone(timedelta(hours=5, minutes=30))


async def sync_badges(session: AsyncSession, student_id: uuid.UUID) -> dict[str, datetime]:
    """Stores any newly earned badge, returns {badge_id: earned_at} of all
    earned ones. earned_at = start of the first voice-test session that
    scored >= 80% in the badge's level (now, if that session is gone).
    Caller commits."""
    level_to_badge = {level: badge_id for badge_id, _, level in BADGES}
    rows = await session.execute(
        select(StudentTestResultBloomScore.bloom_level, func.min(AiAssessorSession.created_at))
        .join(StudentTestResult, StudentTestResult.id == StudentTestResultBloomScore.student_test_result_id)
        .outerjoin(
            AiAssessorSession,
            (AiAssessorSession.student_id == StudentTestResult.student_id)
            & (AiAssessorSession.test_id == StudentTestResult.test_id),
        )
        .where(
            StudentTestResult.student_id == student_id,
            StudentTestResultBloomScore.bloom_level.in_(list(level_to_badge)),
            StudentTestResultBloomScore.percent >= BADGE_THRESHOLD,
        )
        .group_by(StudentTestResultBloomScore.bloom_level)
    )
    now = datetime.now(timezone.utc)
    new = [
        {"student_id": student_id, "badge_id": level_to_badge[level], "earned_at": earned_at or now}
        for level, earned_at in rows.all()
    ]
    if new:
        await session.execute(insert(StudentBadge).values(new).on_conflict_do_nothing())
    stored = await session.execute(
        select(StudentBadge.badge_id, StudentBadge.earned_at).where(StudentBadge.student_id == student_id)
    )
    return dict(stored.all())


def _week_start(now: datetime) -> datetime:
    local = now.astimezone(IST)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


async def class_quests(session: AsyncSession, student_id: uuid.UUID, now: datetime | None = None) -> list[dict]:
    """The student's class quests for the current (IST, Monday-start) week:
    - this week's latest voice test finished by 80% of the class
    - the class answers 50 homework questions (sum of total_questions of
      homework confirmed complete this week)
    [] when the student has no class."""
    class_id = await session.scalar(select(StudentProfile.class_id).where(StudentProfile.user_id == student_id))
    if class_id is None:
        return []
    week_start = _week_start(now or datetime.now(timezone.utc))
    classmates = select(StudentProfile.user_id).where(StudentProfile.class_id == class_id)
    quests = []

    test_id = await session.scalar(
        select(VoiceTest.id)
        .where(VoiceTest.class_id == class_id, VoiceTest.status != TestStatus.DRAFT, VoiceTest.created_at >= week_start)
        .order_by(VoiceTest.created_at.desc())
        .limit(1)
    )
    if test_id is not None:
        class_size = await session.scalar(select(func.count()).select_from(classmates.subquery()))
        finished = await session.scalar(
            select(func.count(func.distinct(StudentTestResult.student_id))).where(
                StudentTestResult.test_id == test_id, StudentTestResult.student_id.in_(classmates)
            )
        )
        target = max(1, math.ceil(class_size * VOICE_TEST_QUEST_SHARE))
        quests.append({
            "id": f"quest-voice-test-{test_id}",
            "title": "80% of the class finishes this week's voice test",
            "progress": min(finished, target),
            "target": target,
        })

    # ponytail: counts questions of confirmed homework; switch to per-question
    # answer rows once homework answers (C6) are stored.
    answered = await session.scalar(
        select(func.coalesce(func.sum(Homework.total_questions), 0))
        .join(StudentHomeworkProgress, StudentHomeworkProgress.homework_id == Homework.id)
        .where(StudentHomeworkProgress.student_id.in_(classmates), StudentHomeworkProgress.confirmed_at >= week_start)
    )
    quests.append({
        "id": f"quest-homework-{week_start.date().isoformat()}",
        "title": f"The class answers {HOMEWORK_QUEST_TARGET} homework questions this week",
        "progress": min(int(answered), HOMEWORK_QUEST_TARGET),
        "target": HOMEWORK_QUEST_TARGET,
    })
    return quests


def boss_concept_for(report: dict | None, concepts_by_question_number: dict[int, list[str]]) -> str | None:
    """The concept of the student's lowest-scoring voice-test answer - the
    homework "boss question" topic. `report` is the stored grader report
    (AiAssessorSessionRecord.report, entries under "qa_transcripts"),
    `concepts_by_question_number` maps VoiceTestQuestion.order -> its
    concepts. None when nothing was scored or the question has no concept."""
    scored = [
        e for e in (report or {}).get("qa_transcripts") or []
        if isinstance(e, dict) and isinstance(e.get("score_percent"), (int, float))
        and concepts_by_question_number.get(e.get("question_number"))
    ]
    if not scored:
        return None
    worst = min(scored, key=lambda e: (e["score_percent"], e["question_number"]))
    return concepts_by_question_number[worst["question_number"]][0]
