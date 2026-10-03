"""Writes the real outcome of a completed AI Assessor session tied to a
VoiceTest - the missing link flagged in NEXT_STEP.md: before this, nothing
in the codebase ever transitioned a VoiceTest to COMPLETED/RESULTS_READY or
wrote StudentTestResult/StudentTestResultBloomScore/StudentPoints, so
school_analytics.py's mastery/Bloom/trend numbers and the leaderboard read
as if no test had ever happened, and homework.py's `test.status ==
RESULTS_READY` gate made "generate follow-up Homework from test gaps"
unreachable in production - only ever exercised via hand-seeded test
fixtures.

On top of that, also fans out to homework_autogen.auto_generate_homework -
per product direction, a student's own completed session is their
"learning path," and Homework is the automatic next step on it rather than
something that waits on a teacher noticing the result and running the
manual generate/review/assign flow (homework.py still has that flow,
unchanged, for whole-class intervention).

Also upserts StudentChapterProgress for the Test's chapter - the "Journey"
chapter map (src/api/routes/journey.py) reads that table to decide which
chapters are DONE/CURRENT/LOCKED, and before this fix nothing anywhere
ever wrote to it, so no chapter could ever advance past the first no
matter how many Tests a student completed. Stars are computed via
stars.py's shared threshold function - the same one nool-apps'
computeSessionReward.ts uses client-side for the win screen's star
reveal, so the number the student sees there matches what Journey stores.

`mastery_percent` is a completion-rate placeholder (percent of questions
answered), an honesty-first formula given no LLM/grading vendor exists yet
(see content_generator.py's docstring) - not a real correctness grade.
Points awarded follow the same principle: 10 points per question actually
answered, a real participation signal rather than a synthesized "score."

VoiceTest.status moves straight to RESULTS_READY, collapsing the
COMPLETED/RESULTS_PROCESSING steps the model's docstring describes as
"never collapsed" - defensible only because there is currently no real
async grading step to occupy that gap; revisit once real grading needs
one. `completed_count` increments once per distinct student (idempotent on
replay, e.g. a client reconnect near the end of the same session) - a
correction to a UI-visible number that was silently stuck at 0 for every
Voice Test ever created, matching `assigned_count`'s separately-tracked
issue (never populated at test-creation time - out of scope here, since
fixing it means resolving WHOLE_CLASS/SPECIFIC_STUDENTS target membership
into a real count, a distinct piece of work).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    BloomLevel,
    StudentChapterProgress,
    StudentPoints,
    StudentTestResult,
    StudentTestResultBloomScore,
    TestStatus,
    VoiceTest,
)
from src.services.stars import stars_for_mastery_percent

POINTS_PER_ANSWERED_QUESTION = 10


async def record_test_completion(
    session: AsyncSession,
    *,
    student_id: uuid.UUID,
    test_id: uuid.UUID,
    answered: int,
    total_questions: int,
    bloom_levels: list[BloomLevel],
    mastery_percent: int | None = None,
    bloom_scores: dict[BloomLevel, int] | None = None,
) -> int | None:
    """`mastery_percent`/`bloom_scores` - real per-level scores from
    evaluation_service.evaluate_colearner_session(...), passed by
    ai_assessor.py's stream_session when colearner's report scored the
    session. When omitted (any other/older caller, or a report that didn't
    score), falls back to the original completion-rate placeholder below -
    unchanged, non-breaking.
    """
    if mastery_percent is None:
        mastery_percent = round(100 * answered / total_questions) if total_questions else 0

    existing = await session.execute(
        select(StudentTestResult).where(
            StudentTestResult.test_id == test_id, StudentTestResult.student_id == student_id
        )
    )
    result_row = existing.scalar_one_or_none()
    already_completed = result_row is not None
    if result_row is None:
        result_row = StudentTestResult(test_id=test_id, student_id=student_id, mastery_percent=mastery_percent)
        session.add(result_row)
    else:
        result_row.mastery_percent = mastery_percent
    await session.flush()

    await session.execute(
        delete(StudentTestResultBloomScore).where(
            StudentTestResultBloomScore.student_test_result_id == result_row.id
        )
    )
    for level in bloom_levels or [BloomLevel.UNDERSTAND]:
        level_percent = bloom_scores.get(level, mastery_percent) if bloom_scores else mastery_percent
        session.add(
            StudentTestResultBloomScore(
                student_test_result_id=result_row.id, bloom_level=level, percent=level_percent
            )
        )

    homework_due = False
    test = await session.get(VoiceTest, test_id)
    if test is not None:
        if not already_completed:
            test.completed_count += 1
        if test.status in (TestStatus.DRAFT, TestStatus.SCHEDULED, TestStatus.ACTIVE):
            test.status = TestStatus.RESULTS_READY

    if not already_completed:
        points_row = await session.get(StudentPoints, student_id)
        awarded = answered * POINTS_PER_ANSWERED_QUESTION
        if points_row is None:
            session.add(StudentPoints(student_id=student_id, points=awarded))
        else:
            points_row.points += awarded

        if test is not None:
            # The follow-up homework is NOT built here: it can take minutes
            # (question bank + LLM) and must never hold up - or on failure roll
            # back - the student's results. The caller commits first, then
            # schedules it (homework_autogen.schedule_auto_homework).
            homework_due = True

            chapter_progress_result = await session.execute(
                select(StudentChapterProgress).where(
                    StudentChapterProgress.student_id == student_id,
                    StudentChapterProgress.chapter_id == test.chapter_id,
                )
            )
            chapter_progress = chapter_progress_result.scalar_one_or_none()
            stars = stars_for_mastery_percent(mastery_percent)
            if chapter_progress is None:
                session.add(
                    StudentChapterProgress(
                        student_id=student_id,
                        chapter_id=test.chapter_id,
                        stars=stars,
                        completed_at=datetime.now(timezone.utc),
                    )
                )
            else:
                chapter_progress.stars = stars
                chapter_progress.completed_at = datetime.now(timezone.utc)

    # mastery for the follow-up homework, when one is due (first completion only)
    return mastery_percent if homework_due else None
