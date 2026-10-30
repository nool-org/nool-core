import datetime as dt
from typing import Literal

from pydantic import Field

from src.api.schemas.common import CamelModel
from src.api.schemas.daily_practice import PracticeItemOut

Stage = Literal["quiz", "homework_pending", "homework", "recheck", "mastered", "needs_teacher"]


class TopicLoopOut(CamelModel):
    topic_id: str
    topic_name: str
    taught_on: dt.date
    stage: Stage
    quiz_id: str | None
    recheck_id: str | None
    homework_id: str | None
    quiz_score: int | None
    recheck_score: int | None
    topic_page: int | None = None  # the NCERT page it starts on


class TopicLoopsOut(CamelModel):
    topics: list[TopicLoopOut]


class QuizStartIn(CamelModel):
    attempt: int = Field(default=1, ge=1, le=2)


class QuizAnalysisOut(CamelModel):
    score_percent: int | None  # None: nothing was answered
    per_bloom: dict[str, int]
    previous_per_bloom: dict[str, int] | None
    lagging: list[str]
    summary: str
    lagging_meaning: str
    need_to_know: list[str]
    next_step: str
    outcome: Literal["homework", "mastered", "needs_teacher"]
    homework_id: str | None
    asked: dict[str, int] = {}  # answers per level; a level asked once is 'Not assessed' (S-012), none stored = old quiz


class TopicQuizOut(CamelModel):
    id: str
    topic_id: str
    topic_name: str
    attempt: int
    status: Literal["open", "completed"]
    items: list[PracticeItemOut]
    xp_awarded: int
    analysis: QuizAnalysisOut | None
    quiz_test_id: str | None = None  # an attempt at a teacher's test (topic_id is then "")
    # the NCERT page to read: the topic's, or for a teacher's test its weakest topic's
    topic_page: int | None = None


class QuizFinishOut(CamelModel):
    analysis: QuizAnalysisOut
    xp_awarded: int
    streak_days: int


class NeedsTeacherOut(CamelModel):
    student_id: str
    name: str
    lagging: str | None


class TopicQuizSummaryOut(CamelModel):
    topic_id: str
    topic_name: str
    student_count: int
    quizzed: int
    mastered: int
    average_percent: int | None
    per_bloom: dict[str, int]
    weakest_level: str | None
    needs_teacher: list[NeedsTeacherOut]


class ChapterTopicQuizzesOut(CamelModel):
    topics: list[TopicQuizSummaryOut]


class SuggestedTopicOut(CamelModel):
    topic_id: str
    name: str


class SuggestedFormatOut(CamelModel):
    format: str  # voice | fix_steps
    label: str
    why: str
    fix_cards: int = 0  # voice: Fix Milo cards to mix into the talk
    topics: list[SuggestedTopicOut] = []  # the topics worked on paper (what Fix Milo covers)


class FocusTopicOut(CamelModel):
    topic_id: str
    name: str
    average_percent: int | None


class ChallengeSuggestionOut(CamelModel):
    """What the teacher could set for this chapter and class, and why - best fit first."""
    nature: str
    taken: bool  # the class already has every challenge it could get on this chapter
    options: list[SuggestedFormatOut]
    bloom_levels: list[str]
    focus_topics: list[FocusTopicOut]
    evidence: list[str]
    note: str | None
