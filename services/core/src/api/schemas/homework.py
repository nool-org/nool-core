from pydantic import Field

from src.domain.models import BloomLevel, HomeworkDifficulty, HomeworkStatus

from .bloom import BloomTarget
from .common import CamelModel
from .roster import AssignmentTarget


class HomeworkOut(CamelModel):
    id: str
    test_id: str
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
    # names of the targeted students (SPECIFIC_STUDENTS), in the same order as target.student_ids
    target_student_names: list[str] | None = None


class CreateHomeworkIn(CamelModel):
    test_id: str
    dataset_ids: list[str]
    # bounded: each question may cost an LLM call, and a non-positive count used to hang slot planning
    total_questions: int = Field(ge=1, le=30)
    difficulty: HomeworkDifficulty
    bloom_distribution: list[BloomTarget]


class AssignHomeworkIn(CamelModel):
    target: AssignmentTarget
    completion_window_hours: int


class HomeworkQuestionOut(CamelModel):
    id: str
    homework_id: str
    order: int
    bloom_level: BloomLevel
    dataset_id: str
    text: str
    answer: str


class UpdateHomeworkQuestionIn(CamelModel):
    """PATCH: send text, answer or both."""
    text: str | None = Field(default=None, min_length=1)
    answer: str | None = Field(default=None, min_length=1)
