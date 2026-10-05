from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shared.auth import AuthenticatedUser
from shared.errors import AppError

from src.api.deps import get_current_user
from src.services import insights_service

router = APIRouter(prefix="/api/v1", tags=["insights"])


class InsightsUnavailableError(AppError):
    code = "INSIGHTS_UNAVAILABLE"
    status_code = 503


class InsightLevel(BaseModel):
    level: str
    percent: int


class InsightTopic(BaseModel):
    name: str
    percent: int


class InsightCardIn(BaseModel):
    question_number: int
    question_text: str
    student_response: str = ""
    expected_points: list[str] = []
    missed_points: list[str] = []
    topic: str | None = None
    subtopic: str | None = None


class InsightsRequest(BaseModel):
    """Stateless, like /homework-insight: everything needed is in the body."""
    grade: str | None = None
    subject: str | None = None
    chapter: str | None = None
    mastery_percent: int
    per_bloom: list[InsightLevel] = []
    topics: list[InsightTopic] = []
    cards: list[InsightCardIn] = []


class InsightCardOut(BaseModel):
    question_number: int
    key_idea: str


class NextStep(BaseModel):
    title: str
    reference: str


class InsightsResponse(BaseModel):
    headline: str
    cards: list[InsightCardOut]
    next_steps: list[NextStep] = Field(max_length=3)  # core pads from templates if fewer
    recap_text: str


@router.post("/insights", response_model=InsightsResponse)
async def create_insights(
    payload: InsightsRequest,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> InsightsResponse:
    try:
        return InsightsResponse(**await insights_service.generate_insights(payload.model_dump()))
    except Exception as exc:
        # core falls back to template words on any non-2xx
        insights_service.logger.warning("insights_generation_failed", extra={"reason": str(exc)[:300]})
        raise InsightsUnavailableError("Could not write insights right now.") from exc
