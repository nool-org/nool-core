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
    is_boss: bool = False
    gap_concept: str | None = None
    figure_svg: str | None = None  # a picture question's drawing (SVG markup)
    figure_url: str | None = None  # or its image file (ASSET_BASE_URL)


class McqAnsweredOut(CamelModel):
    question_id: str
    option_index: int
    correct: bool
    correct_index: int
    explanation: str


class McqTestOut(CamelModel):
    id: str
    test_id: str
    title: str
    questions: list[McqQuestionOut]
    speed_mode: bool = False
    lifelines_left: list[str] = []
    # already-answered questions (first answers), so the app can resume
    answered: list[McqAnsweredOut] = []
    # questions still being built (a minute or so) - the app asks again shortly
    preparing: bool = False


class SpeedModeIn(CamelModel):
    on: bool


class SpeedModeOut(CamelModel):
    speed_mode: bool


class McqAnswerIn(CamelModel):
    question_id: str
    option_index: int = Field(ge=-1, le=3)  # -1: the time ran out, no option chosen
    elapsed_ms: int | None = Field(default=None, ge=0)


class McqAnswerOut(CamelModel):
    correct: bool
    correct_index: int
    explanation: str
    combo: int  # consecutive correct answers in question order, ending at this one
    xp_so_far: int


class LifelineIn(CamelModel):
    kind: Literal["fifty_fifty", "ask_milo"]
    question_id: str


class LifelineOut(CamelModel):
    hide_option_indexes: list[int] | None = None  # fifty_fifty
    hint: str | None = None  # ask_milo


class McqAnswer(CamelModel):
    question_id: str
    option_index: int = Field(ge=-1, le=3)


class McqAttemptIn(CamelModel):
    # optional: answers recorded per question while playing count first; legacy clients send them all here
    answers: list[McqAnswer] = []


class XpBreakdownOut(CamelModel):
    correct: int
    combo_bonus: int
    boss_bonus: int
    speed_bonus: int
    lifeline_cost: int
    total: int


class GapsFixedOut(CamelModel):
    fixed: int
    total: int
    concepts: list[str]


class RankChangeOut(CamelModel):
    before: int
    after: int


class GrowthOut(CamelModel):
    key: str
    kind: Literal["bloom", "concept"]
    before: int | None  # None: the voice test never scored this level ("Not assessed")
    after_corrected: int
    delta: int | None


class McqAttemptOut(CamelModel):
    score_percent: int
    per_bloom: dict[str, int]
    growth: list[GrowthOut]
    correct_answers: list[McqAnswer]
    # overall mastery at the voice test vs now (guess-corrected), and XP this attempt earned
    mastery_before: int | None = None
    mastery_after: int | None = None
    xp_awarded: int = 0
    xp_breakdown: XpBreakdownOut | None = None
    best_combo: int = 0
    gaps_fixed: GapsFixedOut | None = None
    # "you beat X% of your class" - null under 3 classmates' completed checks
    class_percentile: int | None = None
    rank: RankChangeOut | None = None  # class leaderboard rank before/after this attempt's XP
    missed_question_ids: list[str] = []


class StudentGrowthOut(CamelModel):
    student_id: str
    name: str
    before: int | None
    after: int | None
    delta: int | None
    # still under FLAG_BELOW after homework + re-check: the teacher should step in
    flagged: bool = False


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
