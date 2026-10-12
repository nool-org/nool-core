import logging

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from shared.auth import AuthenticatedUser
from shared.errors import AppError

from src.api.deps import get_current_user, nool_caller
from src.domain.schemas import SessionConfig
from src.services.evaluation import grade_transcript
from src.services.evaluation.grader import grade_answer
from src.services.cost_service import cost_header
from src.services.realtime_service import _transcript_text, build_rubric
from src.services.session_store import SessionState

router = APIRouter(prefix="/api/v1", tags=["colearner-regrade"])
logger = logging.getLogger(__name__)

MAX_TURNS = 500


class RegradeIn(BaseModel):
    """A finished session as Core stored it (contract C3 finish payload):
    the session config (its `questions` are the rubric), the transcript turns
    and the per-question answers (for hints_used and the version asked)."""
    config: SessionConfig
    transcript: list[dict] = Field(..., max_length=MAX_TURNS)
    answers: list[dict] = Field(default_factory=list, max_length=20)


@router.post("/colearner/regrade", summary="Re-run the grader on a stored transcript")
async def regrade(body: RegradeIn, response: Response, _user: AuthenticatedUser = Depends(get_current_user),
                  caller: dict = Depends(nool_caller)) -> dict:
    """Returns the C2 report, exactly as a live finish would - on failure it
    carries `grading_error` (the caller decides what to do). X-Nool-Cost carries
    the grader call's cost for Core to persist."""
    turns = [{**t, "role": t.get("role"), "content": str(t.get("content") or t.get("text") or ""),
              "question_number": t.get("question_number")}
             for t in body.transcript if isinstance(t, dict)]
    answers = {a.get("question_number"): a for a in body.answers if isinstance(a, dict)}
    rubric = build_rubric(body.config.questions, answers)
    session = SessionState("regrade", body.config)
    report = await grade_transcript(session, _transcript_text(turns), rubric=rubric)
    logger.info("regrade_cost", extra={"cost": {"usd": round(session.total_cost_usd, 6), **caller}})
    if header := cost_header(session.turn_metrics):
        response.headers["X-Nool-Cost"] = header
    return report


class GradeAnswerIn(BaseModel):
    """One short spoken practice answer (Core's daily practice, practice_voice.py)."""
    question: str = Field(..., min_length=1, max_length=2000)
    expected_points: list[str] = Field(default_factory=list, max_length=10)
    misconceptions: list[str] = Field(default_factory=list, max_length=10)
    transcript: str = Field(..., max_length=5000)
    subject: str | None = Field(default=None, max_length=100)


class GradeAnswerOut(BaseModel):
    score: int
    good: str
    add: str


class GradingUnavailableError(AppError):
    code = "GRADING_UNAVAILABLE"
    status_code = 503


@router.post("/colearner/grade-answer", response_model=GradeAnswerOut, summary="Grade one short spoken answer")
async def grade_one_answer(body: GradeAnswerIn, response: Response,
                           _user: AuthenticatedUser = Depends(get_current_user),
                           caller: dict = Depends(nool_caller)) -> GradeAnswerOut:
    """503 when the model call fails - the caller keeps the transcript ungraded.
    X-Nool-Cost (also on a 503 after a billed call) carries the call's cost for Core to persist."""
    metrics: list[dict] = []
    try:
        result = GradeAnswerOut(**await grade_answer(body.question, body.expected_points, body.misconceptions,
                                                     body.transcript, body.subject, caller=caller, metrics=metrics))
    except Exception as exc:
        logger.warning("grade_answer_failed", extra={"reason": f"{type(exc).__name__}: {exc}"})
        error = GradingUnavailableError("Could not grade this answer.")
        if header := cost_header(metrics):
            error.headers = {"X-Nool-Cost": header}
        raise error from exc
    if header := cost_header(metrics):
        response.headers["X-Nool-Cost"] = header
    return result
