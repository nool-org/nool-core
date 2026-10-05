"""Clears a needs_review attempt (grading failed at finish - see
ai_assessor.py's _record_completion): either re-run the grader on the stored
transcript, or record the teacher's manual score. Both record the result
through test_completion.record_test_completion, as a normal finish does."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import AppError, ConflictError, NotFoundError

from src.api.routes.ai_assessor import (
    _colearner_config,
    _question_count,
    _test_questions,
)
from src.api.routes.gamification import apply_xp, session_xp
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionBloomLevel,
    AiAssessorSessionRecord,
    StudentTestResult,
    StudentTestResultBloomScore,
    User,
    VoiceTest,
)
from src.repositories import audit_repository
from src.services.colearner_client import ColearnerUnavailableError, regrade_transcript
from src.services.evaluation_service import evaluate_colearner_session, topic_scores
from src.services.homework_autogen import schedule_auto_homework
from src.services.test_completion import record_test_completion


class RegradeFailedError(AppError):
    code = "REGRADE_FAILED"
    status_code = 502


async def _pending_review(db: AsyncSession, test: VoiceTest, session_id: str):
    try:
        ai_session = await db.get(AiAssessorSession, uuid.UUID(session_id))
    except ValueError:
        ai_session = None
    if ai_session is None or ai_session.test_id != test.id:
        raise NotFoundError("Attempt not found.")
    record = await db.get(AiAssessorSessionRecord, ai_session.id)
    if record is None or not record.needs_review:
        raise ConflictError("This attempt has already been graded.")
    bloom_levels = list((await db.execute(select(AiAssessorSessionBloomLevel.bloom_level).where(
        AiAssessorSessionBloomLevel.session_id == ai_session.id))).scalars().all())
    return ai_session, record, bloom_levels


def _answered_and_hints(record: AiAssessorSessionRecord, has_questions: bool) -> tuple[int, int]:
    answers = [a for a in record.answers or [] if isinstance(a, dict)]
    hints = sum(int(a.get("hints_used") or 0) for a in answers)
    if has_questions:
        return sum(1 for a in answers if a.get("first_answer")), hints
    student_turns = sum(1 for t in record.transcript or [] if isinstance(t, dict) and t.get("role") == "user")
    return max(student_turns - 1, 0), hints  # first student turn is the "ready?" reply


async def _record(db, ai_session, record, bloom_levels, *, hints_used: int, answered: int, num_questions: int,
                  **scores) -> int | None:
    # the failed attempt already counted the student in completed_count
    # (_record_completion); record_test_completion counts them again on a first result
    if (await db.execute(select(StudentTestResult.id).where(
            StudentTestResult.test_id == ai_session.test_id,
            StudentTestResult.student_id == ai_session.student_id))).first() is None:
        test = await db.get(VoiceTest, ai_session.test_id)
        test.completed_count = max(test.completed_count - 1, 0)
    record.needs_review = False
    homework_mastery = await record_test_completion(
        db, student_id=ai_session.student_id, test_id=ai_session.test_id, answered=min(answered, num_questions),
        total_questions=num_questions, bloom_levels=bloom_levels, **scores)
    if homework_mastery is not None:  # no daily goal: the teacher's clear-up isn't the student's day
        await apply_xp(db, ai_session.student_id, session_xp(hints_used))
    return homework_mastery


def _after_commit(ai_session, bloom_levels, homework_mastery):
    if homework_mastery is not None:
        schedule_auto_homework(test_id=ai_session.test_id, student_id=ai_session.student_id,
                               mastery_percent=homework_mastery, bloom_levels=bloom_levels)


async def regrade(db: AsyncSession, test: VoiceTest, session_id: str, teacher: User, token: str) -> int:
    """Re-runs the grader; returns the recorded mastery. 502 (needs_review
    kept) when grading fails again."""
    ai_session, record, bloom_levels = await _pending_review(db, test, session_id)
    questions = await _test_questions(db, test.id)
    config, num_questions = await _colearner_config(db, ai_session, bloom_levels)
    try:
        report = await regrade_transcript(config=config, transcript=record.transcript or [],
                                          answers=record.answers or [], token=token)
    except ColearnerUnavailableError:
        report = None
    evaluation = None if not isinstance(report, dict) or report.get("grading_error") \
        else evaluate_colearner_session(report, bloom_levels)
    if evaluation is None:
        raise RegradeFailedError("Milo couldn't grade this attempt right now. Please try again later, "
                                 "or enter a score yourself.")
    mastery_percent, bloom_scores = evaluation
    scores = {"mastery_percent": mastery_percent, "bloom_scores": bloom_scores}
    if questions:
        scores["topic_scores"] = topic_scores(report, {q.order: q.topic_id for q in questions})
    record.report = report
    answered, hints_used = _answered_and_hints(record, bool(questions))
    homework_mastery = await _record(db, ai_session, record, bloom_levels, hints_used=hints_used,
                                     answered=answered, num_questions=num_questions, **scores)
    await audit_repository.record(db, actor_id=teacher.id, action="test_review_regrade",
                                  target_type="ai_assessor_session", target_id=str(ai_session.id),
                                  detail=f"mastery {mastery_percent}%")
    await db.commit()
    _after_commit(ai_session, bloom_levels, homework_mastery)
    return mastery_percent


async def resolve(db: AsyncSession, test: VoiceTest, session_id: str, teacher: User, mastery_percent: int,
                  note: str | None) -> None:
    """Records the teacher's own score - mastery only, no per-Bloom/topic scores."""
    ai_session, record, bloom_levels = await _pending_review(db, test, session_id)
    questions = await _test_questions(db, test.id)
    answered, hints_used = _answered_and_hints(record, bool(questions))
    homework_mastery = await _record(db, ai_session, record, bloom_levels, hints_used=hints_used, answered=answered,
                                     num_questions=_question_count(questions, bloom_levels),
                                     mastery_percent=mastery_percent, topic_scores={})
    # record_test_completion always writes per-level rows (falling back to mastery) - a manual score has none
    result_id = (await db.execute(select(StudentTestResult.id).where(
        StudentTestResult.test_id == test.id, StudentTestResult.student_id == ai_session.student_id))).scalar_one()
    await db.execute(delete(StudentTestResultBloomScore).where(
        StudentTestResultBloomScore.student_test_result_id == result_id))
    await audit_repository.record(db, actor_id=teacher.id, action="test_review_resolve",
                                  target_type="ai_assessor_session", target_id=str(ai_session.id),
                                  detail=f"mastery {mastery_percent}%" + (f": {note}" if note else ""))
    await db.commit()
    _after_commit(ai_session, bloom_levels, homework_mastery)
