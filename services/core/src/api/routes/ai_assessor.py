import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from sqlalchemy import exists, select, update
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
from src.domain.models.voice_test import VoiceTestPrepStatus
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
    StudentTestResultBloomScore,
    VoiceTestTargetStudent,
)
from src.repositories import get_session
from src.services import ai_usage, topic_quiz
from src.services.colearner_client import create_realtime_call, finish_realtime_call
from src.api.routes.gamification import apply_xp, session_xp
from src.services.evaluation_service import evaluate_colearner_session, topic_scores
from src.services.homework_autogen import schedule_auto_homework
from src.services.test_completion import record_test_completion

router = APIRouter(prefix="/api/v1", tags=["ai-assessor"])
logger = get_logger(__name__)

VOICE_TEST_GOAL_KEY = "goal-finish-voice-test"
# colearner hangs a call up by 8 min + 3 min warm-up + grace; past this an unfinished call was abandoned
ABANDONED_AFTER_SECONDS = 15 * 60
_background: set[asyncio.Task] = set()


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
        if await db.scalar(select(VoiceTest.id).where(VoiceTest.id == body.test_id, VoiceTest.still_open())) is None:
            raise ConflictError("This test's deadline has passed, so it can't be taken any more.")
        prep_status = await db.scalar(select(VoiceTest.prep_status).where(VoiceTest.id == body.test_id))
        if prep_status == VoiceTestPrepStatus.PENDING:
            raise ConflictError("Milo is still getting this test's questions ready. Try again in a minute.")
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

    return OpenSessionOut(session_id=str(ai_session.id),
                          question_count=len(await _test_questions(db, body.test_id)))


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
            "hints": q.hints or [], "easier": q.easier_version,
        } for q in questions]
    student = await db.get(User, ai_session.student_id)
    if student and (first_name := (student.display_name or "").split()[:1]):
        config["student_name"] = first_name[0].title()
    if student_context := await _student_context(db, ai_session):
        config["student_context"] = student_context
    if test and student and (focus := await topic_quiz.chapter_focus(db, student, test.chapter_id)):
        config["student_focus"] = focus
    return config, num_questions


async def _student_context(db: AsyncSession, ai_session: AiAssessorSession) -> str | None:
    """One line on the student's last earlier voice test, for Milo to pitch
    the session: "Last test: strong at X, found Y hard." None without one."""
    query = (select(StudentTestResult.id).join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
             .where(StudentTestResult.student_id == ai_session.student_id)
             .order_by(VoiceTest.created_at.desc()).limit(1))
    if ai_session.test_id is not None:
        query = query.where(StudentTestResult.test_id != ai_session.test_id)
    last_result = await db.scalar(query)
    if last_result is None:
        return None
    scores = sorted((await db.execute(
        select(StudentTestResultBloomScore.percent, StudentTestResultBloomScore.bloom_level).where(
            StudentTestResultBloomScore.student_test_result_id == last_result,
            StudentTestResultBloomScore.percent.is_not(None))
    )).all(), key=lambda row: row[0])
    if not scores:
        return None
    best, weakest = scores[-1][1].value.lower(), scores[0][1].value.lower()
    if len(scores) == 1:
        return f"Last test: scored {scores[0][0]}% on {best} questions."
    return f"Last test: strong at {best} questions, found {weakest} questions hard."[:300]


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


async def _count_confusion(db: AsyncSession, test_id, result: dict) -> None:
    """The quality loop across students: every question this session asked
    counts once in asked_count, and in confused_count when the student asked
    for it again twice, went silent, or never answered. Once asked twice and
    confusing at least half the time it is flagged needs_rewording for the
    teacher. Single UPDATEs, so concurrent finishes never lose a count."""
    # asked = answered, or Milo got to it (transcript turns carry its number) - not the ones time ran out on
    reached = {t.get("question_number") for t in result.get("transcript") or result.get("turns") or []
               if isinstance(t, dict)}
    confused: dict[uuid.UUID, bool] = {}
    for a in result.get("answers") or []:
        if not isinstance(a, dict) or not (a.get("first_answer") or a.get("question_number") in reached):
            continue
        try:
            question_id = uuid.UUID(str(a.get("question_id")))
        except ValueError:
            continue
        confused[question_id] = confused.get(question_id, False) or (
            int(a.get("repeat_requests") or 0) >= 2 or int(a.get("silences") or 0) >= 1 or not a.get("first_answer"))
    for question_id, was_confused in confused.items():
        asked = VoiceTestQuestion.asked_count + 1
        confusions = VoiceTestQuestion.confused_count + int(was_confused)
        await db.execute(update(VoiceTestQuestion).where(
            VoiceTestQuestion.id == question_id, VoiceTestQuestion.voice_test_id == test_id,
        ).values(asked_count=asked, confused_count=confusions, needs_rewording=(asked >= 2) & (confusions * 2 >= asked)))


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
        if ai_session.test_id is not None:
            await _count_confusion(completion_db, ai_session.test_id, result)
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
    _watch_for_abandon(ai_session.id, answer["call_id"], _bearer(authorization))
    return RealtimeAnswerOut(sdp=answer["sdp"], call_id=answer["call_id"])


def _watch_for_abandon(session_id: uuid.UUID, call_id: str, token: str) -> None:
    """The app was killed mid-test (no finish ever comes): the call still cost
    voice minutes. Once it's surely over, finish it here like an `exited` one -
    its cost recorded, the transcript kept, no score. A finished session
    already has its record, so this does nothing.
    ponytail: in-process timer, lost on a Core restart; a sweep over sessions
    without a record (call_id stored) if that ever matters."""
    async def run() -> None:
        await asyncio.sleep(ABANDONED_AFTER_SECONDS)
        try:
            async with get_session() as db:
                if await db.get(AiAssessorSessionRecord, session_id) is not None:
                    return
            result = await finish_realtime_call(call_id=call_id, owner_ref=str(session_id), token=token)
            await ai_usage.record("colearner", "/api/v1/colearner/realtime/calls/finish", "voice_test",
                                  cost=ai_usage.realtime_cost(result.get("cost")))
            async with get_session() as db:
                ai_session = await db.get(AiAssessorSession, session_id)
                if ai_session is not None and await db.get(AiAssessorSessionRecord, session_id) is None:
                    await _store_record(db, ai_session, result)
                    await db.commit()
            logger.info("ai_assessor_abandoned_call_recorded", extra={"session_id": str(session_id)})
        except Exception:  # best effort: the student's attempt is unaffected either way
            logger.warning("ai_assessor_abandoned_call_failed", extra={"session_id": str(session_id)}, exc_info=True)

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


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
    # the whole live call's cost (voice + transcription + grading), persisted once here
    await ai_usage.record("colearner", "/api/v1/colearner/realtime/calls/finish", "voice_test",
                          cost=ai_usage.realtime_cost(result.get("cost")))
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
