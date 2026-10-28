from datetime import datetime
from typing import Literal

from src.api.schemas.common import CamelModel
from src.domain.models import BloomLevel


class InsightBloom(CamelModel):
    level: BloomLevel
    percent: int


class InsightTopic(CamelModel):
    topic_id: str
    name: str
    percent: int
    status: Literal["strong", "ok", "weak"]


class InsightCard(CamelModel):
    question_number: int
    question_text: str
    you_said: str
    key_idea: str
    missed_points: list[str]
    # the grader's score for this answer - the app's "One thing to fix" is the lowest
    score_percent: int | None = None


class InsightNextStep(CamelModel):
    title: str
    reference: str


class TestInsightsOut(CamelModel):
    """Contract C4. Numbers come from stored results; words from colearner
    (or template words when it is unavailable)."""

    test_id: str
    headline: str
    mastery_percent: int
    per_bloom: list[InsightBloom]
    topics: list[InsightTopic]
    cards: list[InsightCard]
    next_steps: list[InsightNextStep]
    recap_text: str
    generated_at: datetime
