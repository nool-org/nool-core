from pydantic import Field

from .common import CamelModel


class ResolveReviewIn(CamelModel):
    mastery_percent: int = Field(..., ge=0, le=100)
    note: str | None = Field(default=None, max_length=1000)


class ReviewClearedOut(CamelModel):
    session_id: str
    mastery_percent: int
