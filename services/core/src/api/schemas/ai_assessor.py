from typing import Literal

from pydantic import Field

from src.domain.models import BloomLevel

from .common import CamelModel


class OpenSessionIn(CamelModel):
    test_id: str
    context_label: str
    bloom_levels: list[BloomLevel]
    duration_seconds: int


class OpenSessionOut(CamelModel):
    session_id: str


class SessionSummary(CamelModel):
    questions_answered: int
    total_questions: int
    elapsed_seconds: int


# ---- Live (WebRTC) voice - see ai_assessor.py's realtime_offer/finish_session.


class RealtimeOfferIn(CamelModel):
    sdp: str


class RealtimeAnswerOut(CamelModel):
    sdp: str
    call_id: str


class FinishSessionIn(CamelModel):
    call_id: str
    # True when the student left early - the call is ended and its transcript kept, but no result recorded.
    exited: bool = False
    # Only ever lowers the XP awarded (50 - 5 per hint), so trusting it is safe.
    hints_used: int = Field(default=0, ge=0)


class FinishSessionOut(CamelModel):
    outcome: Literal["completed", "timed_out", "exited"]
    summary: SessionSummary
