from datetime import datetime
from typing import Literal

from pydantic import Field

from src.api.schemas.common import CamelModel


class McqTestItemOut(CamelModel):
    id: str
    test_id: str
    title: str
    status: Literal["locked", "available", "completed"]
    unlocks_at: datetime | None
    question_count: int


class McqTestListOut(CamelModel):
    items: list[McqTestItemOut]


class McqQuestionOut(CamelModel):
    id: str
    text: str
    options: list[str]
    bloom_level: str
    concept: str


class McqTestOut(CamelModel):
    id: str
    test_id: str
    title: str
    questions: list[McqQuestionOut]


class McqAnswer(CamelModel):
    question_id: str
    option_index: int = Field(ge=0, le=3)


class McqAttemptIn(CamelModel):
    answers: list[McqAnswer]


class GrowthOut(CamelModel):
    key: str
    kind: Literal["bloom", "concept"]
    before: int
    after_corrected: int
    delta: int


class McqAttemptOut(CamelModel):
    score_percent: int
    per_bloom: dict[str, int]
    growth: list[GrowthOut]
    correct_answers: list[McqAnswer]


class StudentGrowthOut(CamelModel):
    student_id: str
    name: str
    before: int | None
    after: int | None
    delta: int | None


class LevelGrowthOut(CamelModel):
    level: str
    before: int
    after: int
    delta: int


class ConceptGrowthOut(CamelModel):
    concept: str
    before: int
    after: int
    delta: int


class TestGrowthOut(CamelModel):
    test_id: str
    students: list[StudentGrowthOut]
    per_bloom: list[LevelGrowthOut]
    concepts: list[ConceptGrowthOut]
