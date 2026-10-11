"""Daily practice: a recorded voice answer to one item - see
src/services/practice_voice.py. The audio is processed in memory only."""

from fastapi import APIRouter, Depends, Header
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.common import CamelModel
from src.domain.models import Role, User
from src.services import practice_voice

router = APIRouter(prefix="/api/v1", tags=["daily-practice"])


class VoiceAnswerIn(CamelModel):
    audio_base64: str = Field(min_length=1, max_length=(5 * 1024 * 1024 * 4) // 3 + 4)
    mime_type: str = Field(default="audio/m4a", max_length=50)


class VoiceFeedbackOut(CamelModel):
    good: str
    add: str


class VoiceAnswerOut(CamelModel):
    transcript: str | None
    score: int | None  # None: couldn't be graded - the teacher sees the transcript
    feedback: VoiceFeedbackOut
    xp_awarded: int


@router.post("/me/daily-practice/{session_id}/items/{item_id}/voice", summary="Answer a voice item by recording")
async def answer_voice_item(
    session_id: str,
    item_id: str,
    body: VoiceAnswerIn,
    authorization: str | None = Header(default=None),
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> VoiceAnswerOut:
    """Transcribe + grade + XP. Repeating an answered item returns the stored
    result. 503 TRANSCRIPTION_UNAVAILABLE: retry the same recording."""
    token = (authorization or "").split(" ", 1)[-1].strip()
    result = await practice_voice.answer_voice_item(
        session, session_id, item_id, user, audio_base64=body.audio_base64, mime_type=body.mime_type, token=token)
    return VoiceAnswerOut(**result)
