import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shared.auth import AuthenticatedUser

from src.api.deps import get_current_user, nool_caller
from src.domain.schemas import SessionConfig
from src.services import realtime_service

router = APIRouter(prefix="/api/v1", tags=["colearner-realtime"])
logger = logging.getLogger(__name__)


MAX_SDP_CHARS = 20_000  # a browser/RN WebRTC offer is ~3-8k
MAX_OWNER_REF_CHARS = 128


class CreateCallIn(BaseModel):
    sdp: str = Field(..., max_length=MAX_SDP_CHARS)
    config: SessionConfig
    # Caller's own id for this session (Core's AiAssessorSession id) - finish
    # must present the same one, so a call id alone can't fetch a report.
    owner_ref: str = Field(..., min_length=1, max_length=MAX_OWNER_REF_CHARS)


class CreateCallOut(BaseModel):
    sdp: str
    call_id: str
    # carries on this session's earlier, unfinished call (app reloaded / connection dropped)
    resumed: bool = False
    # resumed only: test seconds left on the clock (the app's timer carries on from here)
    seconds_left: int | None = None


class FinishCallIn(BaseModel):
    owner_ref: str = Field(..., min_length=1, max_length=MAX_OWNER_REF_CHARS)


@router.post("/colearner/realtime/calls", response_model=CreateCallOut, summary="Start a live voice call")
async def create_call(body: CreateCallIn, _user: AuthenticatedUser = Depends(get_current_user),
                      caller: dict = Depends(nool_caller)) -> CreateCallOut:
    """WebRTC 'unified interface': the client's SDP offer in, OpenAI's SDP
    answer out. See realtime_service's module docstring."""
    try:
        answer, call = await realtime_service.create_call(
            sdp_offer=body.sdp, config=body.config, owner_ref=body.owner_ref
        )
    except realtime_service.RealtimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    call_id = call.call_id
    # joins realtime_call_cost (logged by call_id at hang-up) to the student - scripts/cost_report.py
    logger.info("realtime_call_started call_id=%s owner_ref=%s student=%s school=%s feature=%s", call_id,
                body.owner_ref, caller.get("student", ""), caller.get("school", ""), caller.get("feature", ""))
    left = max(round(realtime_service.call_time_limit(body.config) - call.clock_used), 0) if call.resumed else None
    return CreateCallOut(sdp=answer, call_id=call_id, resumed=call.resumed, seconds_left=left)


@router.post("/colearner/realtime/calls/{call_id}/finish", summary="End a live voice call and score it")
async def finish_call(
    call_id: str, body: FinishCallIn, _user: AuthenticatedUser = Depends(get_current_user)
) -> dict:
    try:
        return await realtime_service.finish_call(call_id, body.owner_ref)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown or expired call.") from exc
