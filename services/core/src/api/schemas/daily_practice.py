import datetime as dt
from typing import Literal

from pydantic import Field

from src.api.schemas.common import CamelModel


class ChapterRefOut(CamelModel):
    id: str
    name: str


class McqResultOut(CamelModel):
    correct: bool
    correct_index: int
    explanation: str


class VoiceFeedbackOut(CamelModel):
    good: str = ""
    add: str = ""


class VoiceResultOut(CamelModel):
    transcript: str | None
    score: int | None
    feedback: VoiceFeedbackOut


class PracticeItemOut(CamelModel):
    id: str
    order: int
    kind: Literal["mcq", "voice"]
    topic_name: str | None
    bloom_level: str
    review: bool
    text: str
    options: list[str] | None
    answered: bool
    result: McqResultOut | VoiceResultOut | None


class DailyPracticeTodayOut(CamelModel):
    session_id: str | None
    date: dt.date  # Asia/Kolkata
    chapter: ChapterRefOut | None
    status: Literal["open", "completed", "none"]
    items: list[PracticeItemOut]
    xp_awarded: int
    streak_days: int
    empty_reason: Literal["no_topics_taught", "no_questions"] | None


class PracticeMcqAnswerIn(CamelModel):
    option_index: int = Field(ge=0, le=3)
    elapsed_ms: int | None = Field(default=None, ge=0)


class PracticeMcqAnswerOut(CamelModel):
    correct: bool
    correct_index: int
    explanation: str
    xp_awarded: int
    combo: int  # correct MCQs in a row ending at this one


class MasteryChangeOut(CamelModel):
    topic_name: str
    before: int
    after: int


class DailyPracticeCompleteOut(CamelModel):
    xp_awarded: int  # the completion bonus
    streak_days: int
    mastery: list[MasteryChangeOut]


class TopicProgressOut(CamelModel):
    id: str
    name: str
    order: int
    taught_on: dt.date | None


class ChapterProgressOut(CamelModel):
    chapter: ChapterRefOut
    topics: list[TopicProgressOut]


class TopicTaughtIn(CamelModel):
    taught: bool
    taught_on: dt.date | None = None  # default today (Asia/Kolkata)


class SummaryStudentOut(CamelModel):
    id: str
    name: str
    practised_today: bool
    streak_days: int
    last_practised_on: dt.date | None


class WeakTopicOut(CamelModel):
    topic_id: str
    topic_name: str
    struggling_percent: int


class SlippingStudentOut(CamelModel):
    student_id: str
    name: str
    reason: str


class DailyPracticeSummaryOut(CamelModel):
    date: dt.date
    current_chapter: ChapterRefOut | None
    student_count: int
    practised_count: int
    students: list[SummaryStudentOut]
    weakest_topics: list[WeakTopicOut]
    slipping: list[SlippingStudentOut]
