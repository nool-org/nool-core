import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from shared.auth import AuthenticatedUser
from shared.errors import UnauthorizedError

from src.api.deps import get_current_user
from src.domain.schemas import CoLearnerRequest, CoLearnerResponse, SessionConfig
from src.services.audio_service import decode_base64_audio
from src.services.chained_service import process_chained_turn
from src.services.session_store import session_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["colearner-chained"])


@router.post("/colearner/chained", response_model=CoLearnerResponse, summary="Chained Co-Learner Turn")
async def handle_chained_turn(
    payload: CoLearnerRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CoLearnerResponse:
    """
    Turn-based Chained Voice Co-Learner endpoint (Whisper STT -> gpt-4o-mini -> Edge-TTS).
    - On Turn 0 (init): send JSON config (with textbook_context & reference_questions).
      Returns the initial greeting text + audio.
    - On subsequent turns: send session_id and user_audio_base64 (or user_text).
      Returns student transcript, AI reply, audio base64, and the final report on completion.
    """
    if payload.session_id:
        session = session_store.get_session(payload.session_id, owner_uid=user.uid)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session '{payload.session_id}' not found or expired.")
    else:
        config = payload.config or SessionConfig()
        session = session_store.create_session(config=config, owner_uid=user.uid)

    user_bytes = decode_base64_audio(payload.user_audio_base64) if payload.user_audio_base64 else None

    transcript, ai_text, audio_b64, is_completed, report, turn_metrics, cost_report, is_hint_reply = await process_chained_turn(
        session=session,
        user_audio_bytes=user_bytes,
        user_text=payload.user_text,
        time_limit_reached=payload.time_limit_reached,
        is_hint_request=payload.is_hint_request,
    )

    session_store.save_session(session)

    return CoLearnerResponse(
        session_id=session.session_id,
        turn_index=session.turn_index,
        student_transcript=transcript,
        co_learner_text=ai_text,
        co_learner_audio_base64=audio_b64,
        audio_mime_type="audio/mpeg",
        is_completed=is_completed,
        report=report,
        turn_metrics=turn_metrics,
        cost_report=cost_report,
        is_hint_reply=is_hint_reply,
    )


@router.websocket("/colearner/chained/stream")
async def stream_chained_session(websocket: WebSocket, token: str = Query(...)) -> None:
    """
    Live streaming WebSocket connection for the Chained Voice Co-Learner.
    Auth: pass the Firebase ID token as the ?token= query param (browsers
    cannot set an Authorization header on the WS handshake) - same pattern
    as services/core/src/api/routes/ai_assessor.py's stream_session.

    Protocol:
    1. Client connects with ?token=<firebase-id-token>.
    2. Client sends initial JSON: {"config": {...}}.
    3. Server responds with the Turn 0 greeting.
    4. Client sends {"user_audio_base64": "..."} or {"user_text": "..."} per turn.
    5. Server responds with student transcript, AI reply, audio, and report when completed.
    """
    # accept() must happen before any close() with a custom code: Starlette
    # sends a bare HTTP 403 (no WS close frame, no code) for a close() sent
    # before accept() - the handshake never completed as a WebSocket, so a
    # client can't observe 4401 unless we accept first. See ai_assessor.py's
    # stream_session, which closes before accept() and only gets away with
    # it because its test drives a fake WebSocket, not a real socket.
    await websocket.accept()
    try:
        user = get_current_user(authorization=f"Bearer {token}")
    except UnauthorizedError:
        await websocket.close(code=4401)
        return

    try:
        # Same pydantic model (and size caps) as the REST endpoint.
        init = CoLearnerRequest.model_validate_json(await websocket.receive_text())
        session = session_store.create_session(config=init.config or SessionConfig(), owner_uid=user.uid)

        transcript, ai_text, audio_b64, is_completed, report, turn_metrics, cost_report, is_hint_reply = await process_chained_turn(session, None)
        await websocket.send_json({
            "event": "greeting",
            "session_id": session.session_id,
            "turn_index": session.turn_index,
            "co_learner_text": ai_text,
            "co_learner_audio_base64": audio_b64,
            "is_completed": False,
            "turn_metrics": turn_metrics,
            "is_hint_reply": is_hint_reply,
        })

        while not session.is_completed:
            msg = CoLearnerRequest.model_validate_json(await websocket.receive_text())
            user_bytes = decode_base64_audio(msg.user_audio_base64) if msg.user_audio_base64 else None

            transcript, ai_text, audio_b64, is_completed, report, turn_metrics, cost_report, is_hint_reply = await process_chained_turn(
                session, user_bytes, msg.user_text, msg.time_limit_reached, msg.is_hint_request
            )
            await websocket.send_json({
                "event": "ai_turn" if not is_completed else "session_completed",
                "session_id": session.session_id,
                "turn_index": session.turn_index,
                "student_transcript": transcript,
                "co_learner_text": ai_text,
                "co_learner_audio_base64": audio_b64,
                "is_completed": is_completed,
                "report": report,
                "turn_metrics": turn_metrics,
                "cost_report": cost_report,
                "is_hint_reply": is_hint_reply,
            })
    except WebSocketDisconnect:
        pass
    except ValidationError:
        await websocket.send_json({"event": "error", "detail": "Invalid or oversized message."})
    except Exception:
        # Never echo exception text to the client - it can carry upstream
        # provider details. The log has the full traceback.
        logger.exception("colearner_ws_turn_failed")
        await websocket.send_json({"event": "error", "detail": "Something went wrong. Please try again."})
