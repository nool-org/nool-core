from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shared.auth import AuthenticatedUser

from src.api.deps import get_current_user
from src.domain.schemas import SessionConfig
from src.services.evaluation import grade_transcript
from src.services.realtime_service import _transcript_text, build_rubric
from src.services.session_store import SessionState

router = APIRouter(prefix="/api/v1", tags=["colearner-regrade"])

MAX_TURNS = 500


class RegradeIn(BaseModel):
    """A finished session as Core stored it (contract C3 finish payload):
    the session config (its `questions` are the rubric), the transcript turns
    and the per-question answers (for hints_used and the version asked)."""
    config: SessionConfig
    transcript: list[dict] = Field(..., max_length=MAX_TURNS)
    answers: list[dict] = Field(default_factory=list, max_length=20)


@router.post("/colearner/regrade", summary="Re-run the grader on a stored transcript")
async def regrade(body: RegradeIn, _user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """Returns the C2 report, exactly as a live finish would - on failure it
    carries `grading_error` (the caller decides what to do)."""
    turns = [{**t, "role": t.get("role"), "content": str(t.get("content") or t.get("text") or ""),
              "question_number": t.get("question_number")}
             for t in body.transcript if isinstance(t, dict)]
    answers = {a.get("question_number"): a for a in body.answers if isinstance(a, dict)}
    rubric = build_rubric(body.config.questions, answers)
    return await grade_transcript(SessionState("regrade", body.config), _transcript_text(turns), rubric=rubric)
