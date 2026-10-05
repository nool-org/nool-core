"""Auto-generates and immediately assigns a personal Homework the moment a
student's own Test/Co-learner session completes - see test_completion.py's
call site.

PRODUCT.md's original Homework flow ("Test complete -> results -> teacher
generates -> reviews -> assigns") was teacher-gated end to end: a teacher
had to notice a completed Test and manually run generate/review/assign
before any student ever saw Homework. Per product direction, that gate is
removed for the student's own follow-up - the completing student's AI
Learner session *is* their learning path, and Homework is the automatic
next step on it, targeted at just that student (SPECIFIC_STUDENTS, one
name). A teacher's manual whole-class flow (POST /homework, /generate,
/assign - homework.py) still exists unchanged for anything beyond this
per-student follow-up (e.g. assigning the same intervention to a whole
class after reviewing results).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Homework,
    HomeworkDifficulty,
    HomeworkQuestion,
    HomeworkStatus,
    HomeworkTargetStudent,
    StudentHomeworkProgress,
    StudentTestResult,
    StudentTestResultTopicScore,
    User,
    VoiceTest,
    VoiceTestQuestion,
)
from src.services.homework_builder import Focus, build_homework_questions, grounding_dataset_id

AUTO_HOMEWORK_QUESTION_COUNT = 5
AUTO_HOMEWORK_COMPLETION_WINDOW_HOURS = 48
WEAK_PERCENT = 50  # below this an answer/topic is "weak" (same cut as the insights' "weak" status)


async def auto_generate_homework(
    session: AsyncSession,
    *,
    test: VoiceTest,
    student_id: uuid.UUID,
    mastery_percent: int,
    bloom_levels: list[BloomLevel],
) -> None:
    """No-ops (rather than raising) when there is no Dataset to ground
    questions in - HomeworkQuestion.dataset_id is NOT NULL, the same
    requirement the manual teacher flow has, just with no teacher present
    to pick one. Caller (test_completion.py) only invokes this once per
    (test, student) - guarded by the same `already_completed` check that
    gates the points award, so a WS reconnect replay never double-creates.
    """
    student = await session.get(User, student_id)
    dataset_id = await _pick_dataset(session, user=student, test=test)
    if dataset_id is None:
        return

    chapter = await session.get(Chapter, test.chapter_id)
    gap_topic = chapter.name if chapter is not None else "this chapter"

    hw = Homework(
        test_id=test.id,
        class_id=test.class_id,
        gap_topic=gap_topic,
        gap_mastery_percent=mastery_percent,
        total_questions=AUTO_HOMEWORK_QUESTION_COUNT,
        difficulty=HomeworkDifficulty.MIXED,
        target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS,
        completion_window_hours=AUTO_HOMEWORK_COMPLETION_WINDOW_HOURS,
        status=HomeworkStatus.ASSIGNED,
        assigned_count=1,
    )
    session.add(hw)
    await session.flush()

    session.add(HomeworkTargetStudent(homework_id=hw.id, student_id=student_id))
    session.add(StudentHomeworkProgress(homework_id=hw.id, student_id=student_id))

    student = await session.get(User, student_id)
    drafts = await build_homework_questions(
        session, user=student, test=test, total=AUTO_HOMEWORK_QUESTION_COUNT,
        bloom_weights=_even_bloom_distribution(bloom_levels, AUTO_HOMEWORK_QUESTION_COUNT),
        difficulty=HomeworkDifficulty.MIXED, fallback_dataset_id=dataset_id, seed=str(hw.id),
        student_id=student_id, **await weak_spots(session, test_id=test.id, student_id=student_id),
    )
    for order, d in enumerate(drafts, start=1):
        session.add(
            HomeworkQuestion(
                homework_id=hw.id,
                order=order,
                bloom_level=d.bloom_level,
                dataset_id=d.dataset_id,
                source_question_id=d.source_question_id,
                text=d.text,
                answer=d.answer,
                **({"is_boss": True} if d.is_boss else {}),  # unset = server default false
            )
        )


async def weak_spots(session: AsyncSession, *, test_id, student_id) -> dict:
    """{focus, boss} for build_homework_questions, from this student's own
    graded voice test: answers scored under WEAK_PERCENT or showing a
    misconception point at their test question's topic/subtopic/concepts
    (weakest first); the weakest topic score picks the topic. The boss is
    the lowest-scoring answer's question. Empty when nothing was graded."""
    questions = {q.order: q for q in (await session.execute(
        select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test_id))).scalars()}
    report = (await session.execute(
        select(AiAssessorSessionRecord.report)
        .join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.test_id == test_id, AiAssessorSession.student_id == student_id,
               AiAssessorSessionRecord.needs_review.is_(False))
        .order_by(AiAssessorSessionRecord.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    scored = []  # (score, no misconception, question)
    for entry in (report or {}).get("qa_transcripts") or []:
        question = questions.get(entry.get("question_number")) if isinstance(entry, dict) else None
        if question is not None and isinstance(entry.get("score_percent"), (int, float)):
            scored.append((entry["score_percent"], not entry.get("misconception"), question))
    scored.sort(key=lambda row: row[:2])
    weak = [q for score, clean, q in scored if score < WEAK_PERCENT or not clean]

    weakest_topic = (await session.execute(
        select(StudentTestResultTopicScore.topic_id)
        .join(StudentTestResult, StudentTestResult.id == StudentTestResultTopicScore.student_test_result_id)
        .where(StudentTestResult.test_id == test_id, StudentTestResult.student_id == student_id,
               StudentTestResultTopicScore.percent < WEAK_PERCENT)
        .order_by(StudentTestResultTopicScore.percent).limit(1)
    )).scalar_one_or_none()
    topic_id = weakest_topic or (weak[0].topic_id if weak else None)

    spots = {}
    if topic_id or weak:
        spots["focus"] = Focus(
            topic_id=topic_id,
            subtopic_id=next((q.subtopic_id for q in weak if q.topic_id == topic_id), None),
            concepts=tuple(dict.fromkeys(c for q in weak for c in q.concepts or [])),
        )
    if scored:
        q = scored[0][2]
        spots["boss"] = Focus(topic_id=q.topic_id, subtopic_id=q.subtopic_id, concepts=tuple(q.concepts or []),
                              bloom_level=q.bloom_level)
    return spots


def _even_bloom_distribution(levels: list[BloomLevel], count: int) -> dict[BloomLevel, int]:
    """Spreads `count` questions as evenly as possible across the assessed
    Bloom levels, for the KG grounding call's `bloom_distribution` -
    this auto-generated flow only has a flat list of assessed levels
    (unlike the teacher-driven flow, which has a real per-level count from
    HomeworkBloomDistribution), so there's no real weighting to preserve.
    """
    if not levels:
        return {BloomLevel.UNDERSTAND: count}
    base, remainder = divmod(count, len(levels))
    return {level: base + (1 if i < remainder else 0) for i, level in enumerate(levels)}


# kept under its old name: tests monkeypatch homework_autogen._pick_dataset
_pick_dataset = grounding_dataset_id


# Strong refs so scheduled tasks aren't garbage-collected mid-run.
_background: set = set()


def schedule_auto_homework(*, test_id: uuid.UUID, student_id: uuid.UUID, mastery_percent: int,
                           bloom_levels: list[BloomLevel]) -> None:
    """Build the student's follow-up homework in the background, in its own
    session, after their results are committed - so it can neither delay the
    completion acknowledgement nor roll the results back when it fails."""
    import asyncio
    import logging

    from src.repositories import get_session

    async def run() -> None:
        try:
            async with get_session() as session:
                test = await session.get(VoiceTest, test_id)
                if test is None:
                    return
                await auto_generate_homework(session, test=test, student_id=student_id,
                                             mastery_percent=mastery_percent, bloom_levels=bloom_levels)
                await session.commit()
        except Exception:  # logged, never raised: the test result already stands
            logging.getLogger(__name__).exception(
                "auto_homework_failed", extra={"test_id": str(test_id), "student_id": str(student_id)})

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)
