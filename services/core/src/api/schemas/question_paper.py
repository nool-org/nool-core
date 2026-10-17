from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from src.domain.models import BloomLevel, PaperDifficultyLevel, QuestionPaperStatus, QuestionType

from .bloom import BloomTarget
from .common import CamelModel
from .dataset import DatasetShare
from .school_logo import LogoDataUri


class PaperSection(CamelModel):
    label: str
    question_count: int
    marks_each: int
    question_types: list[str] | None = None  # None = the paper's own questionTypes


class PaperRules(CamelModel):
    allow_internal_choice: bool
    avoid_duplicate_concepts: bool
    respect_chapter_weightage: bool
    avoid_recent_repetition: bool


class PaperDifficultyTarget(CamelModel):
    level: PaperDifficultyLevel
    value: int


class BloomTargetPercent(CamelModel):
    level: BloomLevel
    percent: int


class DifficultyMix(CamelModel):
    """Percents (of marks) per difficulty."""

    easy: int = 0
    medium: int = 0
    hard: int = 0


class PaperValidationOut(CamelModel):
    coverage_percent: int
    marks_accounted_for: int
    duplicate_count: int
    quality_percent: int
    issues: list[str]  # non-empty only when VALIDATION_FAILED


class ChapterMarks(CamelModel):
    """Chapter weightage: how many of the paper's marks come from this chapter."""
    chapter_id: str
    marks: int = Field(ge=0, le=200)


class QuestionPaperOut(CamelModel):
    id: str
    name: str
    exam_type: str
    board: str
    grade: int
    language: str
    subject_id: str
    dataset_shares: list[DatasetShare]
    chapter_ids: list[str]
    chapter_marks: list[ChapterMarks] = []
    topic_ids: list[str]
    total_marks: int | None
    duration_minutes: int | None
    sections: list[PaperSection]
    bloom_distribution: list[BloomTarget]
    difficulty_distribution: list[PaperDifficultyTarget]
    question_types: list[str]
    rules: PaperRules
    status: QuestionPaperStatus
    validation: PaperValidationOut | None
    custom_header_text: str | None = None
    logo_data_uri: str | None = None
    instructions: list[str] | None = None
    # C9: same rows as bloomDistribution/difficultyDistribution, in the
    # Paper Copilot's shape.
    bloom_targets: list[BloomTargetPercent] | None = None
    difficulty_mix: DifficultyMix | None = None


class CreateQuestionPaperIn(CamelModel):
    name: str
    exam_type: str
    board: str
    grade: int
    language: str
    subject_id: str
    chapter_ids: list[str] | None = None
    # weightage for some or all of chapter_ids; chapters missing here get none
    chapter_marks: list[ChapterMarks] | None = Field(default=None, max_length=50)
    topic_ids: list[str] | None = None
    dataset_shares: list[DatasetShare] | None = None
    question_types: list[str] | None = None
    sections: list[PaperSection] | None = None
    total_marks: int | None = None
    duration_minutes: int | None = None
    bloom_distribution: list[BloomTarget] | None = None
    difficulty_distribution: list[PaperDifficultyTarget] | None = None
    custom_header_text: str | None = None
    logo_data_uri: LogoDataUri | None = None
    instructions: list[str] | None = None
    # C9 - win over bloomDistribution/difficultyDistribution when both are sent.
    bloom_targets: list[BloomTargetPercent] | None = None
    difficulty_mix: DifficultyMix | None = None


QuestionSourceName = Literal["bank", "ai", "edited"]


class QuestionPaperQuestionOut(CamelModel):
    id: str
    paper_id: str
    order: int
    bloom_level: BloomLevel
    marks: int
    text: str
    question_type: QuestionType | None = None
    difficulty: PaperDifficultyLevel | None = None
    chapter_id: str | None = None
    topic_id: str | None = None
    section_name: str | None = None
    options: list[str] | None = None
    answer: str | None = None
    source: QuestionSourceName = "ai"
    dataset_question_id: str | None = None


class UpdateQuestionPaperQuestionIn(CamelModel):
    text: str


class QuestionPaperQuestionCandidateOut(CamelModel):
    id: str
    text: str


class ReorderQuestionsIn(CamelModel):
    ordered_question_ids: list[str]


# ---- C11 structured ops ------------------------------------------------------

class NewQuestionIn(CamelModel):
    text: str = Field(min_length=1)
    question_type: QuestionType
    marks: int
    bloom_level: BloomLevel
    difficulty: PaperDifficultyLevel
    chapter_id: str
    topic_id: str | None = None
    options: list[str] | None = None
    answer: str | None = None
    source: Literal["bank", "ai"]
    dataset_question_id: str | None = None


class QuestionChangesIn(CamelModel):
    text: str | None = Field(default=None, min_length=1)
    marks: int | None = None
    options: list[str] | None = None
    answer: str | None = None
    bloom_level: BloomLevel | None = None
    difficulty: PaperDifficultyLevel | None = None
    question_type: QuestionType | None = None


class ReplaceOp(CamelModel):
    op: Literal["replace"]
    question_id: str
    new_question: NewQuestionIn


class AddOp(CamelModel):
    op: Literal["add"]
    section_name: str | None = None
    position: int | None = None  # 1-based position in the whole paper
    new_question: NewQuestionIn


class RemoveOp(CamelModel):
    op: Literal["remove"]
    question_id: str


class EditOp(CamelModel):
    op: Literal["edit"]
    question_id: str
    changes: QuestionChangesIn


class ReorderOp(CamelModel):
    op: Literal["reorder"]
    ordered_question_ids: list[str]


PaperOp = Annotated[ReplaceOp | AddOp | RemoveOp | EditOp | ReorderOp, Field(discriminator="op")]
ActionOrigin = Literal["chat", "quick_action", "manual", "explain"]


class PaperActionsIn(CamelModel):
    ops: list[PaperOp] = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    origin: ActionOrigin


# ---- C10 rules check ---------------------------------------------------------

class MarksTargetOut(CamelModel):
    target: int | None
    actual: int


class SectionCheckOut(CamelModel):
    name: str
    target_marks: int
    actual_marks: int
    target_count: int
    actual_count: int
    ok: bool


class BloomMixOut(CamelModel):
    level: BloomLevel
    target_percent: int | None
    actual_percent: int


class ChapterCoverageOut(CamelModel):
    chapter_id: str
    name: str
    question_count: int
    marks: int


class RepeatOut(CamelModel):
    question_id: str
    matched_in: Literal["paper", "homework", "voice_test"]
    matched_title: str
    matched_at: datetime


class RulesIssueOut(CamelModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    question_id: str | None = None


class RulesCheckOut(CamelModel):
    ok: bool
    total_marks: MarksTargetOut
    sections: list[SectionCheckOut]
    bloom_mix: list[BloomMixOut]
    chapters: list[ChapterCoverageOut]
    difficulty: DifficultyMix
    estimated_minutes: int
    duration_minutes: int | None
    repeats: list[RepeatOut]
    issues: list[RulesIssueOut]


# ---- versions ----------------------------------------------------------------

class PaperActionsOut(CamelModel):
    version_id: str
    version_number: int
    questions: list[QuestionPaperQuestionOut]
    rules_check: RulesCheckOut


class PaperVersionOut(CamelModel):
    id: str
    number: int
    summary: str
    origin: str
    created_at: datetime
    created_by: str


class PaperVersionsOut(CamelModel):
    items: list[PaperVersionOut]
