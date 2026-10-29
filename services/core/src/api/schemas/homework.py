from datetime import datetime
from typing import Literal

from pydantic import Field

from src.domain.models import BloomLevel, HomeworkDifficulty, HomeworkStatus

from .bloom import BloomTarget
from .common import CamelModel
from .roster import AssignmentTarget


class HomeworkOut(CamelModel):
    id: str
    test_id: str | None  # None for homework set straight from a chapter
    chapter_id: str | None = None
    class_id: str
    gap_topic: str
    gap_mastery_percent: int
    dataset_ids: list[str]
    total_questions: int
    difficulty: HomeworkDifficulty
    bloom_distribution: list[BloomTarget]
    target: AssignmentTarget
    completion_window_hours: int | None
    status: HomeworkStatus
    assigned_count: int
    completed_count: int
    chapter_name: str | None = None
    created_at: datetime | None = None  # tells apart two rounds of homework on one chapter
    # names of the targeted students (SPECIFIC_STUDENTS), in the same order as target.student_ids
    target_student_names: list[str] | None = None


class CreateHomeworkIn(CamelModel):
    # either the voice test it follows up, or a class + chapter the teacher sets it on directly
    test_id: str | None = None
    class_id: str | None = None
    chapter_id: str | None = None
    dataset_ids: list[str]
    # bounded: each question may cost an LLM call, and a non-positive count used to hang slot planning
    total_questions: int = Field(ge=1, le=30)
    difficulty: HomeworkDifficulty
    bloom_distribution: list[BloomTarget]


class AssignHomeworkIn(CamelModel):
    target: AssignmentTarget
    completion_window_hours: int = Field(ge=1, le=720)


class StudentHomeworkQuestionOut(CamelModel):
    """Student-facing: never carries the answer (students write their own)."""
    id: str
    homework_id: str
    order: int
    bloom_level: BloomLevel
    dataset_id: str
    text: str
    is_boss: bool = False  # auto homework's double-XP question
    figure_svg: str | None = None  # a picture question's drawing (SVG markup)
    figure_url: str | None = None  # or its image file (ASSET_BASE_URL)


class HomeworkQuestionOut(StudentHomeworkQuestionOut):
    """Teacher-facing: includes the model answer."""
    answer: str


class HomeworkAnswerIn(CamelModel):
    answer_text: str | None = Field(default=None, max_length=4000)
    self_rating: Literal["got_it", "confused"]


class HomeworkAnswerOut(CamelModel):
    saved: bool = True
    xp_awarded: int  # 0 when this question was already answered


class UpdateHomeworkQuestionIn(CamelModel):
    """PATCH: send text, answer or both."""
    text: str | None = Field(default=None, min_length=1)
    answer: str | None = Field(default=None, min_length=1)
