from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from shared.logging import get_logger

from src.api.deps import get_db_session, require_feature
from src.api.schemas.ai_assessor import (
    FinishSessionIn,
    FinishSessionOut,
    OpenSessionIn,
    OpenSessionOut,
    RealtimeAnswerOut,
    RealtimeOfferIn,
    SessionSummary,
)
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionBloomLevel,
    AiAssessorSessionRecord,
    BloomLevel,
    Chapter,
    Feature,
    SchoolClass,
    Subject,
    TestStatus,
    User,
    VoiceTest,
    VoiceTestQuestion,
    Role,
    StudentTestResult,
    VoiceTestTargetStudent,
)
from src.repositories import get_session
from src.services.colearner_client import create_realtime_call, finish_realtime_call
from src.api.routes.gamification import apply_xp, session_xp
from src.services.evaluation_service import evaluate_colearner_session, topic_scores
from src.services.homework_autogen import schedule_auto_homework
from src.services.test_completion import record_test_completion

router = APIRouter(prefix="/api/v1", tags=["ai-assessor"])
logger = get_logger(__name__)

VOICE_TEST_GOAL_KEY = "goal-finish-voice-test"


@router.post("/ai-assessor/sessions", status_code=201, summary="Open a session")
async def open_session(
    body: OpenSessionIn,
    app_user: User = Depends(require_feature(Feature.VOICE_TEST)),
    db: AsyncSession = Depends(get_db_session),
) -> OpenSessionOut:
    if body.test_id is not None:
        # A test session only for a student the test actually targets - it
        # streams the test's content and completing it records results on
        # the test, so it must never open against another class/school's test.
        if app_user.role != Role.STUDENT:
            raise ForbiddenError("Only a student the test is assigned to can take it.")
        targeted = (await db.execute(
            select(VoiceTestTargetStudent.id)
            .join(VoiceTest, VoiceTest.id == VoiceTestTargetStudent.test_id)
            .where(
                VoiceTestTargetStudent.test_id == body.test_id,
                VoiceTestTargetStudent.student_id == app_user.id,
                VoiceTest.status != TestStatus.DRAFT,  # not published yet
            )
        )).first()
        if targeted is None:
            raise NotFoundError("This test isn't assigned to you.")
    ai_session = AiAssessorSession(
        student_id=app_user.id,
        test_id=body.test_id,
        context_label=body.context_label,
        duration_seconds=body.duration_seconds,
    )
    db.add(ai_session)
    await db.flush()
    for level in body.bloom_levels:
        db.add(AiAssessorSessionBloomLevel(session_id=ai_session.id, bloom_level=level))
    await db.commit()

    return OpenSessionOut(session_id=str(ai_session.id))


async def _test_questions(db: AsyncSession, test_id) -> list[VoiceTestQuestion]:
    """The test's questions in the order they're asked (none until prep ran)."""
    if test_id is None:
        return []
    return list((await db.execute(
        select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test_id).order_by(VoiceTestQuestion.order)
    )).scalars().all())


def _question_count(questions: list[VoiceTestQuestion], bloom_levels: list[BloomLevel]) -> int:
    return len(questions) or max(len(bloom_levels), 2)


async def _colearner_config(
    db: AsyncSession, ai_session: AiAssessorSession, bloom_levels: list[BloomLevel]
) -> tuple[dict, int]:
    """colearner's SessionConfig for this session, plus its question count.
    With the test's questions (contract C3 `questions`) the co-learner asks
    exactly those, in order; without (prep found none) it improvises from
    textbook_context."""
    test = await db.get(VoiceTest, ai_session.test_id) if ai_session.test_id else None
    chapter = await db.get(Chapter, test.chapter_id) if test else None
    subject = await db.get(Subject, test.subject_id) if test else None
    school_class = await db.get(SchoolClass, test.class_id) if test else None
    # ponytail: a session started before the create-time prep finished (~10-60s)
    # gets no questions and improvises; mark prep pending on VoiceTest and refuse
    # here if teachers schedule + students start that fast in practice.
    questions = await _test_questions(db, ai_session.test_id)

    num_questions = _question_count(questions, bloom_levels)
    levels = [q.bloom_level.value for q in questions] or [level.value for level in bloom_levels]
    config = {
        "grade": f"Grade {school_class.grade}" if school_class else ai_session.context_label,
        "subject": subject.name if subject else ai_session.context_label,
        "chapter_name": chapter.name if chapter else ai_session.context_label,
        "num_questions": num_questions,
        "blooms_attributes": levels or [BloomLevel.UNDERSTAND.value],
        "session_max_time_seconds": ai_session.duration_seconds,
    }
    # Optional keys are OMITTED, not sent as null - colearner's SessionConfig
    # lists only fall back to their defaults when the key is absent.
    if test and test.textbook_context:
        config["textbook_context"] = test.textbook_context
    if questions:
        config["questions"] = [{
            "id": str(q.id), "order": q.order, "bloom_level": q.bloom_level.value, "spoken_text": q.spoken_text,
            "expected_points": q.expected_points or [], "misconceptions": q.misconceptions or [],
            "hints": q.hints or [],
        } for q in questions]
    return config, num_questions


def _store_record(db: AsyncSession, ai_session: AiAssessorSession, result: dict, needs_review: bool = False):
    """Keeps colearner's finish payload (contract C3) as-is - every finished
    session, scored or not - for review and re-grading."""
    return db.merge(AiAssessorSessionRecord(
        session_id=ai_session.id,
        transcript=result.get("transcript") or result.get("turns"),
        answers=result.get("answers"),
        report=result.get("report"),
        needs_review=needs_review,
    ))


def _awaiting_review(test_id, student_id):
    """A session of this student on this test whose grading failed - their
    attempt is with the teacher (GET /tests/{id}/reviews)."""
    return exists().where(
        AiAssessorSessionRecord.session_id == AiAssessorSession.id,
        AiAssessorSessionRecord.needs_review.is_(True),
        AiAssessorSession.test_id == test_id,
        AiAssessorSession.student_id == student_id,
    )


async def _record_completion(
    ai_session: AiAssessorSession, bloom_levels: list[BloomLevel], result: dict,
    answered: int, num_questions: int, hints_used: int = 0,
) -> None:
    """Stores colearner's finish payload and records the test result with
    per-Bloom and per-topic scores, XP (first completion only) and auto
    homework. A report with `grading_error` scores nothing - no made-up
    mastery: the attempt counts as taken (completed_count, no retake) and
    waits for the teacher's review."""
    report = result.get("report")
    grading_error = isinstance(report, dict) and bool(report.get("grading_error"))
    async with get_session() as completion_db:
        if grading_error:
            logger.warning("ai_assessor_grading_failed_needs_review", extra={
                "session_id": str(ai_session.id), "test_id": str(ai_session.test_id),
                "grading_error": str(report.get("grading_error"))[:300]})
            if ai_session.test_id is not None and not (await completion_db.execute(select(
                exists().where(StudentTestResult.test_id == ai_session.test_id,
                               StudentTestResult.student_id == ai_session.student_id)
                | _awaiting_review(ai_session.test_id, ai_session.student_id)
            ))).scalar():
                test = await completion_db.get(VoiceTest, ai_session.test_id)
                if test is not None:
                    test.completed_count += 1  # also freezes the questions (voice_test.py)
            await _store_record(completion_db, ai_session, result, needs_review=True)
            await completion_db.commit()
            return
        await _store_record(completion_db, ai_session, result)
        if ai_session.test_id is None:
            await completion_db.commit()
            return

        scores = {}
        evaluation = evaluate_colearner_session(report, bloom_levels)
        if evaluation is not None:
            mastery_percent, bloom_scores = evaluation
            scores = {"mastery_percent": mastery_percent, "bloom_scores": bloom_scores}
            questions = await _test_questions(completion_db, ai_session.test_id)
            if questions:
                scores["topic_scores"] = topic_scores(report, {q.order: q.topic_id for q in questions})
        homework_mastery = await record_test_completion(
            completion_db,
            student_id=ai_session.student_id,
            test_id=ai_session.test_id,
            answered=answered,
            total_questions=num_questions,
            bloom_levels=bloom_levels,
            **scores,
        )
        if homework_mastery is not None:  # first completion of this test
            await apply_xp(completion_db, ai_session.student_id, session_xp(hints_used), VOICE_TEST_GOAL_KEY)
        await completion_db.commit()
    if homework_mastery is not None:
        # after the results are saved; runs on its own and never blocks `completed`
        schedule_auto_homework(test_id=ai_session.test_id, student_id=ai_session.student_id,
                               mastery_percent=homework_mastery, bloom_levels=bloom_levels)


async def _owned_session(db: AsyncSession, session_id: str, app_user: User) -> tuple[AiAssessorSession, list[BloomLevel]]:
    ai_session = await db.get(AiAssessorSession, session_id)
    if ai_session is None or ai_session.student_id != app_user.id:
        raise NotFoundError("Voice session not found.")
    bloom_levels = list((await db.execute(
        select(AiAssessorSessionBloomLevel.bloom_level).where(AiAssessorSessionBloomLevel.session_id == ai_session.id)
    )).scalars().all())
    return ai_session, bloom_levels


def _bearer(authorization: str | None) -> str:
    return (authorization or "").split(" ", 1)[-1].strip()


@router.post("/ai-assessor/sessions/{session_id}/realtime", summary="Connect live voice (WebRTC offer/answer)")
async def realtime_offer(
    session_id: str,
    body: RealtimeOfferIn,
    authorization: str | None = Header(default=None),
    app_user: User = Depends(require_feature(Feature.VOICE_TEST)),
    db: AsyncSession = Depends(get_db_session),
) -> RealtimeAnswerOut:
    """The app's WebRTC SDP offer in, the live call's SDP answer out. Audio
    then flows directly between the app and the voice model; colearner
    records the transcript server-side (see its realtime_service.py)."""
    ai_session, bloom_levels = await _owned_session(db, session_id, app_user)
    if ai_session.test_id is not None and (await db.execute(
        select(_awaiting_review(ai_session.test_id, ai_session.student_id))
    )).scalar():
        raise ConflictError("Your answers to this test are with your teacher for review.")
    config, _ = await _colearner_config(db, ai_session, bloom_levels)
    answer = await create_realtime_call(
        sdp=body.sdp, config=config, owner_ref=str(ai_session.id), token=_bearer(authorization)
    )
    return RealtimeAnswerOut(sdp=answer["sdp"], call_id=answer["call_id"])


@router.post("/ai-assessor/sessions/{session_id}/finish", summary="End live voice and record the result")
async def finish_session(
    session_id: str,
    body: FinishSessionIn,
    authorization: str | None = Header(default=None),
    app_user: User = Depends(require_feature(Feature.VOICE_TEST)),
    db: AsyncSession = Depends(get_db_session),
) -> FinishSessionOut:
    ai_session, bloom_levels = await _owned_session(db, session_id, app_user)
    num_questions = _question_count(await _test_questions(db, ai_session.test_id), bloom_levels)
    result = await finish_realtime_call(call_id=body.call_id, owner_ref=str(ai_session.id), token=_bearer(authorization))
    answered = min(int(result.get("answered") or 0), num_questions)
    # colearner counts hints server-side per question; the app's own count only adds to it
    hints_used = max(body.hints_used, sum(
        int(a.get("hints_used") or 0) for a in result.get("answers") or [] if isinstance(a, dict)))
    elapsed = int((datetime.now(timezone.utc) - ai_session.created_at).total_seconds())
    summary = SessionSummary(questions_answered=answered, total_questions=num_questions, elapsed_seconds=elapsed)

    if body.exited:
        async with get_session() as record_db:
            await _store_record(record_db, ai_session, result)
            await record_db.commit()
        return FinishSessionOut(outcome="exited", summary=summary)
    await _record_completion(ai_session, bloom_levels, result, answered, num_questions, hints_used=hints_used)
    finished = result.get("model_completed") or answered >= num_questions
    return FinishSessionOut(outcome="completed" if finished else "timed_out", summary=summary)
