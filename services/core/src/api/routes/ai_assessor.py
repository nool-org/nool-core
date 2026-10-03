import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ForbiddenError, NotFoundError, UnauthorizedError
from shared.logging import get_logger

from src.api.deps import get_current_user, get_db_session, require_feature
from src.api.schemas.ai_assessor import (
    CompletedEvent,
    ConnectionEvent,
    ErrorEvent,
    ErrorPayload,
    ExitedEvent,
    FinishSessionIn,
    FinishSessionOut,
    HintEvent,
    OpenSessionIn,
    OpenSessionOut,
    ProgressEvent,
    QuestionEvent,
    QuestionPayload,
    RealtimeAnswerOut,
    RealtimeOfferIn,
    SessionSummary,
    TimedOutEvent,
    TimerEvent,
    TranscriptEvent,
    TurnEvent,
)
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionBloomLevel,
    BloomLevel,
    Chapter,
    Feature,
    School,
    SchoolClass,
    Subject,
    TestStatus,
    User,
    VoiceTest,
    Role,
    VoiceTestTargetStudent,
)
from src.repositories import get_by_firebase_uid, get_session
from src.services.colearner_client import (
    ColearnerSession,
    ColearnerUnavailableError,
    create_realtime_call,
    finish_realtime_call,
)
from src.api.routes.gamification import apply_xp, session_xp
from src.services.evaluation_service import evaluate_colearner_session
from src.services.homework_autogen import schedule_auto_homework
from src.services.test_completion import record_test_completion

router = APIRouter(prefix="/api/v1", tags=["ai-assessor"])
logger = get_logger(__name__)

_DEFAULT_PIPELINE = "chained"
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

    return OpenSessionOut(
        session_id=str(ai_session.id),
        ws_url=f"/api/v1/ai-assessor/sessions/{ai_session.id}/stream",
    )


def _headline_from(text: str, *, limit: int = 80) -> str:
    """A short label for QuestionPayload.headline - colearner returns one
    spoken block of text, not a separate headline/prompt pair, so this is
    a plain truncation to the first sentence (or `limit` chars) rather
    than a second LLM call just for a UI label.
    """
    for delimiter in (". ", "? ", "! "):
        if delimiter in text:
            candidate = text.split(delimiter, 1)[0].strip()
            if candidate:
                return candidate[:limit]
    return text.strip()[:limit]


async def _colearner_config(
    db: AsyncSession, ai_session: AiAssessorSession, bloom_levels: list[BloomLevel]
) -> tuple[dict, int]:
    """colearner's SessionConfig for this session, plus its question count."""
    test = await db.get(VoiceTest, ai_session.test_id) if ai_session.test_id else None
    chapter = await db.get(Chapter, test.chapter_id) if test else None
    subject = await db.get(Subject, test.subject_id) if test else None
    school_class = await db.get(SchoolClass, test.class_id) if test else None

    num_questions = max(len(bloom_levels), 2)
    config = {
        "grade": f"Grade {school_class.grade}" if school_class else ai_session.context_label,
        "subject": subject.name if subject else ai_session.context_label,
        "chapter_name": chapter.name if chapter else ai_session.context_label,
        "num_questions": num_questions,
        "blooms_attributes": [level.value for level in bloom_levels] or [BloomLevel.UNDERSTAND.value],
        "session_max_time_seconds": ai_session.duration_seconds,
    }
    # Both keys must be OMITTED, not set to None, when there's nothing to
    # ground the session in - colearner's SessionConfig.reference_questions
    # is a plain (non-Optional) list with a default_factory, so an explicit
    # `null` fails Pydantic validation instead of falling back to it (only
    # a genuinely absent key does). See Phase 2's _generate_reference_content
    # in voice_test.py for when these are actually populated.
    if test and test.textbook_context:
        config["textbook_context"] = test.textbook_context
    if test and test.reference_questions:
        config["reference_questions"] = test.reference_questions
    return config, num_questions


async def _record_completion(
    ai_session: AiAssessorSession, bloom_levels: list[BloomLevel], final_report: dict | None,
    answered: int, num_questions: int, hints_used: int = 0,
) -> None:
    """Scores the colearner report and records the test result, XP (first
    completion only) and auto homework. No-op for a session not tied to a test."""
    if ai_session.test_id is None:
        return
    evaluation = evaluate_colearner_session(final_report, bloom_levels)
    scores = {}
    if evaluation is not None:
        mastery_percent, bloom_scores = evaluation
        scores = {"mastery_percent": mastery_percent, "bloom_scores": bloom_scores}
    async with get_session() as completion_db:
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
    num_questions = max(len(bloom_levels), 2)
    result = await finish_realtime_call(call_id=body.call_id, owner_ref=str(ai_session.id), token=_bearer(authorization))
    answered = min(int(result.get("answered") or 0), num_questions)
    elapsed = int((datetime.now(timezone.utc) - ai_session.created_at).total_seconds())
    summary = SessionSummary(questions_answered=answered, total_questions=num_questions, elapsed_seconds=elapsed)

    if body.exited:
        return FinishSessionOut(outcome="exited", summary=summary)
    await _record_completion(
        ai_session, bloom_levels, result.get("report"), answered, num_questions, hints_used=body.hints_used
    )
    finished = result.get("model_completed") or answered >= num_questions
    return FinishSessionOut(outcome="completed" if finished else "timed_out", summary=summary)


@router.websocket("/ai-assessor/sessions/{session_id}/stream")
async def stream_session(websocket: WebSocket, session_id: str, token: str = Query(...)) -> None:
    """One JSON frame per message, both directions - see
    src/api/schemas/ai_assessor.py for the exact event shapes, matching
    services/voice/AiAssessorSession.ts's AiAssessorEvent. A translating
    relay onto the real colearner service (services/colearner) - see
    src/services/colearner_client.py's module docstring for why Core
    bridges rather than pointing the app at colearner directly.
    """
    try:
        auth_user = get_current_user(authorization=f"Bearer {token}")
    except UnauthorizedError:
        await websocket.close(code=4401)
        return

    async with get_session() as db:
        result = await db.execute(select(AiAssessorSession).where(AiAssessorSession.id == session_id))
        ai_session = result.scalar_one_or_none()
        if ai_session is None:
            await websocket.close(code=4404)
            return

        bloom_result = await db.execute(
            select(AiAssessorSessionBloomLevel.bloom_level).where(
                AiAssessorSessionBloomLevel.session_id == ai_session.id
            )
        )
        bloom_levels = [row[0] for row in bloom_result.all()]

        app_user = await get_by_firebase_uid(db, auth_user.uid)
        if app_user is None or ai_session.student_id != app_user.id:
            # only the student who opened the session may stream it
            await websocket.close(code=4403)
            return
        school = (
            await db.get(School, app_user.school_id) if app_user and app_user.school_id else None
        )

        colearner_config, num_questions = await _colearner_config(db, ai_session, bloom_levels)

    pipeline = (school.voice_pipeline if school else None) or _DEFAULT_PIPELINE

    await websocket.accept()
    await websocket.send_json(ConnectionEvent(state="connecting").model_dump(by_alias=True))

    # Timed explicitly - "the app feels slow to start" is otherwise a guess
    # between three real network hops (this WS handshake already happened;
    # next is Core's own WS to colearner, then colearner's Turn-0 greeting,
    # which includes a real Edge-TTS synthesis call) - this makes it
    # visible in the logs which one actually dominates, instead of
    # re-guessing every time it's reported slow.
    connect_started_at = time.monotonic()
    try:
        colearner = await ColearnerSession.connect(pipeline=pipeline, token=token)
    except ColearnerUnavailableError as exc:
        await websocket.send_json(
            ErrorEvent(error=ErrorPayload(code=exc.code, message=exc.message, retryable=True)).model_dump(by_alias=True)
        )
        await websocket.close(code=1011)
        return
    logger.info(
        "colearner_connect_completed",
        extra={"session_id": session_id, "duration_ms": round((time.monotonic() - connect_started_at) * 1000)},
    )

    await websocket.send_json(ConnectionEvent(state="connected").model_dump(by_alias=True))

    answered = 0
    start_time = asyncio.get_event_loop().time()

    def _elapsed() -> int:
        return int(asyncio.get_event_loop().time() - start_time)

    # Bridges the receive loop (below) and the main send loop, which needs
    # to *wait* for a real "message" or "hint" frame - a student's answer,
    # or an explicit request for a guiding nudge - before forwarding it to
    # colearner and advancing, rather than free-running. pause/resume
    # frames are read and discarded (not implemented server-side) rather
    # than silently counted as an answer.
    inbound: asyncio.Queue = asyncio.Queue()

    async def _receive_loop() -> None:
        try:
            while True:
                frame = await websocket.receive_json()
                await inbound.put(frame)
                if frame.get("type") == "stop":
                    return
        except WebSocketDisconnect:
            await inbound.put({"type": "stop"})

    receiver = asyncio.create_task(_receive_loop())

    async def _await_answer() -> dict | None:
        """Waits for a "message", "hint", or "stop" frame, ignoring
        anything else, bounded by the session's overall duration. None
        means the budget ran out before any of them arrived.
        """
        while True:
            remaining = max(ai_session.duration_seconds - _elapsed(), 0)
            if remaining <= 0:
                return None
            try:
                frame = await asyncio.wait_for(inbound.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if frame.get("type") in ("message", "hint", "stop"):
                return frame

    async def _advance(frame: dict) -> dict | None:
        """Dispatches an already-known 'message' or 'hint' frame to
        colearner and returns its reply frame - or None if colearner itself
        became unavailable (already reported to the client as an
        ErrorEvent). Factored out so both the normal and hint-reply
        branches of the main loop share one dispatch path instead of
        duplicating the try/except.
        """
        try:
            if frame.get("type") == "hint":
                return await colearner.send_hint_request()
            return await colearner.send_turn(text=frame.get("text"), audio_base64=frame.get("audioBase64"))
        except ColearnerUnavailableError as exc:
            await websocket.send_json(
                ErrorEvent(error=ErrorPayload(code=exc.code, message=str(exc), retryable=True)).model_dump(by_alias=True)
            )
            return None

    stopped = False
    timed_out = False
    final_report: dict | None = None
    # colearner's Turn 0 is always a readiness check ("Are you ready to
    # begin?"), not one of the graded questions - its reply is the first
    # student_transcript we see, and must not be counted as an answered
    # question or shown as a QuestionEvent (see _headline_from's docstring
    # neighbor comment below for the parallel on the assessor side).
    readiness_ack_pending = True
    is_first_assessor_turn = True
    question_index = 0

    try:
        greeting_started_at = time.monotonic()
        try:
            colearner_frame = await colearner.send_config(colearner_config)
        except ColearnerUnavailableError as exc:
            await websocket.send_json(
                ErrorEvent(error=ErrorPayload(code=exc.code, message=str(exc), retryable=True)).model_dump(by_alias=True)
            )
            return
        logger.info(
            "colearner_greeting_completed",
            extra={
                "session_id": session_id,
                "pipeline": pipeline,
                "duration_ms": round((time.monotonic() - greeting_started_at) * 1000),
            },
        )

        while True:
            if colearner_frame.get("is_hint_reply"):
                # A guiding nudge, not a new question - the student stays on
                # what they were already answering. No QuestionEvent, no
                # ProgressEvent/TimerEvent bump, no question_index advance;
                # is_first_assessor_turn is untouched too, since this never
                # counts as "the first real question was just asked."
                hint_text = colearner_frame.get("co_learner_text") or ""
                hint_audio = colearner_frame.get("co_learner_audio_base64") or None
                await websocket.send_json(TurnEvent(state="thinking").model_dump(by_alias=True))
                await websocket.send_json(
                    HintEvent(
                        text=hint_text, audio_base64=hint_audio,
                        audio_mime_type="audio/mpeg" if hint_audio else None,
                    ).model_dump(by_alias=True)
                )
                await websocket.send_json(TurnEvent(state="listening").model_dump(by_alias=True))

                frame = await _await_answer()
                if frame is None:
                    timed_out = True
                    try:
                        colearner_frame = await colearner.send_turn(time_limit_reached=True)
                    except ColearnerUnavailableError:
                        break
                    continue
                if frame.get("type") == "stop":
                    stopped = True
                    break
                # Sent *before* the real wait (STT + LLM, several seconds
                # over the real backend), not just after it resolves - see
                # the identical comment at the other _advance() call site
                # below for why this matters.
                await websocket.send_json(TurnEvent(state="thinking").model_dump(by_alias=True))
                next_frame = await _advance(frame)
                if next_frame is None:
                    break
                colearner_frame = next_frame
                continue

            is_completed = bool(colearner_frame.get("is_completed"))
            student_transcript = colearner_frame.get("student_transcript")

            if student_transcript:
                await websocket.send_json(TurnEvent(state="thinking").model_dump(by_alias=True))
                await websocket.send_json(
                    TranscriptEvent(speaker="student", text=student_transcript).model_dump(by_alias=True)
                )
                if readiness_ack_pending:
                    readiness_ack_pending = False
                else:
                    answered += 1
                    await websocket.send_json(
                        ProgressEvent(answered=answered, total=num_questions).model_dump(by_alias=True)
                    )
                    await websocket.send_json(
                        TimerEvent(
                            remaining_seconds=max(ai_session.duration_seconds - _elapsed(), 0),
                            total_seconds=ai_session.duration_seconds,
                        ).model_dump(by_alias=True)
                    )

            if is_completed:
                final_report = colearner_frame.get("report")
                break

            co_text = colearner_frame.get("co_learner_text") or ""
            audio_b64 = colearner_frame.get("co_learner_audio_base64") or None

            await websocket.send_json(TurnEvent(state="speaking").model_dump(by_alias=True))
            if not is_first_assessor_turn:
                bloom_level = bloom_levels[question_index % len(bloom_levels)] if bloom_levels else BloomLevel.UNDERSTAND
                await websocket.send_json(
                    QuestionEvent(
                        question=QuestionPayload(
                            id=f"{session_id}-q{question_index + 1}",
                            index=question_index,
                            total=num_questions,
                            bloom_level=bloom_level,
                            headline=_headline_from(co_text),
                            prompt=co_text,
                        )
                    ).model_dump(by_alias=True)
                )
                question_index += 1
            await websocket.send_json(
                TranscriptEvent(
                    speaker="assessor", text=co_text, audio_base64=audio_b64,
                    audio_mime_type="audio/mpeg" if audio_b64 else None,
                ).model_dump(by_alias=True)
            )
            await websocket.send_json(TurnEvent(state="listening").model_dump(by_alias=True))
            is_first_assessor_turn = False

            frame = await _await_answer()
            if frame is None:
                timed_out = True
                try:
                    colearner_frame = await colearner.send_turn(time_limit_reached=True)
                except ColearnerUnavailableError:
                    break
                continue
            if frame.get("type") == "stop":
                stopped = True
                break

            # Sent *before* the real wait, not after: _advance() is a real
            # STT (for audio answers) + LLM round trip against colearner,
            # which the ai_assessor_stream timing logs above have shown
            # can run several seconds - without this, turnState just sits
            # at 'listening' (the mic already stopped, waveform still
            # animating) the whole time, which reads as frozen/unresponsive
            # rather than "thinking about your answer."
            await websocket.send_json(TurnEvent(state="thinking").model_dump(by_alias=True))
            next_frame = await _advance(frame)
            if next_frame is None:
                break
            colearner_frame = next_frame

        if stopped:
            await websocket.send_json(ExitedEvent().model_dump(by_alias=True))
        else:
            await _record_completion(ai_session, bloom_levels, final_report, answered, num_questions)
            summary = SessionSummary(
                questions_answered=answered, total_questions=num_questions, elapsed_seconds=_elapsed()
            )
            event = TimedOutEvent(summary=summary) if timed_out else CompletedEvent(summary=summary)
            await websocket.send_json(event.model_dump(by_alias=True))
    except WebSocketDisconnect:
        pass
    finally:
        receiver.cancel()
        await colearner.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
