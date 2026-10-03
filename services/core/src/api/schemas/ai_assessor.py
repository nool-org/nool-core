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
    ws_url: str


# ---- WS event payloads - one JSON frame per message, matching
# services/voice/AiAssessorSession.ts's AiAssessorEvent discriminated
# union exactly. Server -> client only; client -> server frames
# (sendMessage/sendAudio/pause/resume/stop) are read as plain dicts in
# the WS route handler, not modeled here, since the handler only branches
# on `type` and never needs to validate/echo them back.


class SessionSummary(CamelModel):
    questions_answered: int
    total_questions: int
    elapsed_seconds: int


class ConnectionEvent(CamelModel):
    type: Literal["connection"] = "connection"
    state: Literal["connecting", "connected", "reconnecting", "disconnected"]


class TurnEvent(CamelModel):
    type: Literal["turn"] = "turn"
    state: Literal["speaking", "listening", "thinking", "paused"]


class QuestionPayload(CamelModel):
    id: str
    index: int
    total: int
    bloom_level: BloomLevel
    headline: str
    prompt: str


class QuestionEvent(CamelModel):
    """Nested under `question`, not flat - matches
    services/voice/AiAssessorSession.ts's `AiAssessorEvent` union exactly
    (`{ type: 'question'; question: AiAssessorQuestion }`), same as
    `CompletedEvent`/`TimedOutEvent` nest under `summary` below. This was
    flat before, which decoded on the client as `event.question ===
    undefined` and crashed the whole session the moment the first question
    arrived (`Cannot read property 'total' of undefined` in
    aiAssessorReducer.ts).
    """

    type: Literal["question"] = "question"
    question: QuestionPayload


class TranscriptEvent(CamelModel):
    type: Literal["transcript"] = "transcript"
    speaker: Literal["assessor", "student"]
    text: str
    # Only ever set for speaker == "assessor" - colearner's synthesized
    # spoken reply (base64 MP3), forwarded unchanged from its own
    # co_learner_audio_base64/audio_mime_type (see
    # services/colearner/src/domain/schemas.py's CoLearnerResponse). None
    # for the student speaker (their audio, if any, was already sent
    # up via the client's own sendAudio call, not echoed back here).
    audio_base64: str | None = None
    audio_mime_type: str | None = None


class HintEvent(CamelModel):
    """A guiding nudge given only because the student explicitly asked for
    one (see CoLearnerActions' hint button on the client) - never a
    correctness validation, and never a new question. No accompanying
    QuestionEvent/ProgressEvent - the client stays on the same question
    (see aiAssessorReducer.ts's 'hint' case) and keeps listening for the
    student's actual answer afterward.
    """

    type: Literal["hint"] = "hint"
    text: str
    audio_base64: str | None = None
    audio_mime_type: str | None = None


class ProgressEvent(CamelModel):
    type: Literal["progress"] = "progress"
    answered: int
    total: int


class TimerEvent(CamelModel):
    type: Literal["timer"] = "timer"
    remaining_seconds: int
    total_seconds: int


class CompletedEvent(CamelModel):
    type: Literal["completed"] = "completed"
    summary: SessionSummary


class TimedOutEvent(CamelModel):
    type: Literal["timed_out"] = "timed_out"
    summary: SessionSummary


class ExitedEvent(CamelModel):
    type: Literal["exited"] = "exited"


class ErrorPayload(CamelModel):
    code: str
    message: str
    retryable: bool = False


class ErrorEvent(CamelModel):
    """Nested under `error` - matches `{ type: 'error'; error: AppError }`
    on the client (AppError's own shape is exactly {code, message,
    retryable}). Not sent anywhere in stream_session today (the
    deterministic content generator never fails), but fixed alongside
    QuestionEvent so it doesn't become the next silent-mismatch landmine
    if a future change adds a real failure path here.
    """

    type: Literal["error"] = "error"
    error: ErrorPayload


# ---- Live (WebRTC) voice - see ai_assessor.py's realtime_offer/finish_session.


class RealtimeOfferIn(CamelModel):
    sdp: str


class RealtimeAnswerOut(CamelModel):
    sdp: str
    call_id: str


class FinishSessionIn(CamelModel):
    call_id: str
    # True when the student left early - the call is ended but nothing is recorded.
    exited: bool = False
    # Only ever lowers the XP awarded (50 - 5 per hint), so trusting it is safe.
    hints_used: int = Field(default=0, ge=0)


class FinishSessionOut(CamelModel):
    outcome: Literal["completed", "timed_out", "exited"]
    summary: SessionSummary
