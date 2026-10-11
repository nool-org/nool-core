from typing import Literal

from src.domain.models import BloomLevel

from .bloom import BloomScore
from .common import CamelModel


class ClassTestResultOut(CamelModel):
    test_id: str
    class_mastery_percent: int
    assigned_count: int
    completed_count: int
    priority_gap_topic: str
    priority_gap_percent: int
    bloom_performance: list[BloomScore]


class StudentTestResultOut(CamelModel):
    test_id: str
    student_id: str
    mastery_percent: int
    bloom_performance: list[BloomScore]


class StudentAnswerOut(CamelModel):
    """One question of the student's session, from its stored record."""

    question_number: int
    bloom_level: BloomLevel | None
    question_text: str
    version: Literal["standard", "easier"]
    nudges_used: int
    score: int | None  # null = not graded
    quote: str  # the student's answer, at most 160 characters; "" when none


class AnswerQuote(CamelModel):
    question_number: int
    quote: str


class AnswerHighlights(CamelModel):
    best: AnswerQuote | None
    weakest: AnswerQuote | None


class StudentTestResultDetailOut(StudentTestResultOut):
    answers: list[StudentAnswerOut]
    highlights: AnswerHighlights
