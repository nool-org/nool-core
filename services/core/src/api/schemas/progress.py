from datetime import datetime

from src.domain.models import BloomLevel

from .bloom import BloomScore
from .common import CamelModel


class SubjectProgressSummaryOut(CamelModel):
    subject_id: str
    subject_label: str
    tests_count: int
    concepts_tracked: int
    score_percent: int
    score_trend_label: str
    strongest_concept: str
    strongest_concept_percent: int
    focus_concept: str
    focus_concept_percent: int


class SubjectTestSummaryOut(CamelModel):
    subject_id: str
    label: str
    test_type: str
    score_percent: int
    trend_label: str
    test_id: str | None = None  # the finished voice test (opens its insights)


class TrajectoryPointOut(CamelModel):
    label: str
    mastery_percent: int


class NextBestFocusOut(CamelModel):
    label: str
    message: str


class ProgressResultOut(CamelModel):
    label: str
    kind: str  # Chat with Milo | Check-in | Second look | Fix Milo's homework | Challenge
    score_percent: int
    at: datetime
    test_id: str | None = None  # a chat with Milo: opens its insights
    quiz_id: str | None = None  # a check-in or challenge: opens its analysis


class SubjectProgressOut(CamelModel):
    """One subject, from everything finished in it (services/student_progress.py)."""
    subject_id: str
    subject_label: str
    score_percent: int
    count: int
    skills: list[BloomScore]
    focus_level: BloomLevel | None = None  # the weakest level, when it's under 70%
    focus_percent: int | None = None
    results: list[ProgressResultOut]


class StudentProgressOverviewOut(CamelModel):
    bloom_mastery: list[BloomScore]
    subject_summaries: list[SubjectProgressSummaryOut]
    subject_tests: list[SubjectTestSummaryOut]
    trajectory: list[TrajectoryPointOut]
    trajectory_gain_label: str
    next_best_focus: NextBestFocusOut
    subjects: list[SubjectProgressOut] = []
