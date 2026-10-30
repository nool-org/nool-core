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


class SpecialResultOut(CamelModel):
    """who_is_right / make_your_own / fix_steps (services/question_types.py)."""
    score: int
    correct: bool
    message: str  # what the answer shows ("HCF(6, 30) is 6 and the LCM is 30, not 36.")
    response: dict  # what the student did
    reveal: dict  # the right choice / an example answer, and the explanation


class PracticeItemOut(CamelModel):
    id: str
    order: int
    kind: Literal["mcq", "voice", "who_is_right", "make_your_own", "fix_steps"]
    topic_name: str | None
    bloom_level: str
    review: bool
    text: str
    options: list[str] | None
    spec: dict | None = None  # the special kinds' question parts - never which choice is right
    figure_svg: str | None = None  # a picture question's drawing (SVG markup)
    figure_url: str | None = None  # or its image file (ASSET_BASE_URL)
    answered: bool
    result: McqResultOut | VoiceResultOut | SpecialResultOut | None


class DailyPracticeTodayOut(CamelModel):
    session_id: str | None
    date: dt.date  # Asia/Kolkata
    chapter: ChapterRefOut | None
    status: Literal["open", "completed", "none"]
    items: list[PracticeItemOut]
    xp_awarded: int
    streak_days: int
    empty_reason: Literal["no_topics_taught", "no_questions"] | None
    new_items: int = 0  # unanswered items the latest extension added (topics taught after the set was built)
    new_topics: list[str] = []  # their topics


class PracticeMcqAnswerIn(CamelModel):
    option_index: int = Field(ge=0, le=3)
    elapsed_ms: int | None = Field(default=None, ge=0)


class PracticeResponseIn(CamelModel):
    response: dict  # who_is_right {pick, reason}; fix_steps {step, fix}; make_your_own {values: [str]}


class PracticeLineIn(CamelModel):
    step: int = Field(ge=0, le=20)


class PracticeLineOut(CamelModel):
    """Fix Milo's first tap: `found` (the circled line is the slip, go on to the fix), else `tryAgain`
    (gentle, nothing revealed), else `result` (second wrong line: scored 0, the usual reveal)."""
    found: bool = False
    try_again: bool = False
    result: SpecialResultOut | None = None
    xp_awarded: int = 0


class PracticeResponseOut(CamelModel):
    result: SpecialResultOut
    xp_awarded: int


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
    answered_today: int = 0  # questions answered in today's set (in progress when < items_today)
    items_today: int = 0
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
