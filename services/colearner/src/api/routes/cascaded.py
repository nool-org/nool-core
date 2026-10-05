import base64

from fastapi import APIRouter, Depends, HTTPException

from shared.auth import AuthenticatedUser

from src.api.deps import get_current_user
from src.domain.schemas import CoLearnerRequest, CoLearnerResponse, SessionConfig
from src.services.audio_service import decode_base64_audio
from src.services.cascaded_service import cost_report, process_cascaded_turn
from src.services.session_store import session_store

router = APIRouter(prefix="/api/v1", tags=["colearner-cascaded"])


@router.post("/colearner/cascaded", response_model=CoLearnerResponse, summary="Cascaded Co-Learner Turn (prototype)")
async def handle_cascaded_turn(payload: CoLearnerRequest, user: AuthenticatedUser = Depends(get_current_user)) -> CoLearnerResponse:
    """Same request/response shape as /colearner/chained; see cascaded_service."""
    if payload.session_id:
        session = session_store.get_session(payload.session_id, owner_uid=user.uid)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session '{payload.session_id}' not found or expired.")
    else:
        session = session_store.create_session(config=payload.config or SessionConfig(), owner_uid=user.uid)

    audio = decode_base64_audio(payload.user_audio_base64) if payload.user_audio_base64 else None
    r = await process_cascaded_turn(session, audio, payload.user_text, payload.time_limit_reached, payload.is_hint_request)
    session_store.save_session(session)
    return CoLearnerResponse(
        session_id=session.session_id,
        turn_index=session.turn_index,
        student_transcript=r["student_transcript"],
        co_learner_text=r["text"],
        co_learner_audio_base64=base64.b64encode(r["audio"]).decode(),
        audio_mime_type="audio/wav",
        is_completed=r["completed"],
        report=r.get("report"),
        turn_metrics=r["timing"],
        cost_report=cost_report(session) if r["completed"] else None,
        is_hint_reply=r.get("hint", False),
    )
