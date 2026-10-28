from datetime import datetime

from pydantic import Field

from src.domain.models import BloomLevel, TestStatus, VoiceTestQuestionStatus

from .bloom import BloomTarget  # noqa: F401 - re-exported for convenience
from .common import CamelModel
from .roster import AssignmentTarget


class VoiceTestOut(CamelModel):
    id: str
    class_id: str
    subject_id: str
    chapter_id: str
    topic_id: str | None  # null for a chapter-level test (every test since topics left the wizard)
    bloom_levels: list[BloomLevel]
    duration_minutes: int
    completion_window_hours: int
    target: AssignmentTarget
    status: TestStatus
    assigned_count: int
    completed_count: int
    created_at: datetime
    due_at: datetime | None = None  # end of the completion window (set once scheduled)
    opens_at: datetime | None = None  # when students can start it (set once scheduled; may be later)
    question_count: int = 0  # prepared questions (0 until prep ran) - not len(bloom_levels)


class RemindIn(CamelModel):
    # None = everyone the test was sent to who hasn't finished
    student_ids: list[str] | None = None


class RemindOut(CamelModel):
    reminded: int  # students told (each gets a push on every device they signed in on)


class ScheduleTestIn(CamelModel):
    # None = open now; a later time keeps it hidden from students until then
    opens_at: datetime | None = None


class CreateTestIn(CamelModel):
    class_id: str
    subject_id: str
    chapter_id: str
    # A test checks the whole chapter; an older client's topic is stored but prep ignores it.
    topic_id: str | None = None
    bloom_levels: list[BloomLevel]
    duration_minutes: int
    completion_window_hours: int
    target: AssignmentTarget
    # Fix Milo cards mixed into the talk - suggested for chapters with equations or calculations
    fix_cards: int = Field(default=0, ge=0, le=2)


class VoiceTestQuestionOut(CamelModel):
    """Contract C1 - one of the questions every student of the test is asked."""

    id: str
    voice_test_id: str
    order: int
    dataset_question_id: str | None
    bloom_level: BloomLevel
    topic_id: str | None
    subtopic_id: str | None
    concepts: list[str]
    original_text: str
    reworded_text: str
    spoken_text: str
    expected_points: list[str]
    misconceptions: list[str]
    hints: list[str]
    card: dict | None = None  # a Fix Milo card (with its answer - this is the teacher's view)
    status: VoiceTestQuestionStatus
    # quality loop across students (ai_assessor.py's _count_confusion) - reword when flagged
    needs_rewording: bool = False
    asked_count: int = 0
    confused_count: int = 0
    has_easier_version: bool = False


class VoiceTestQuestionListOut(CamelModel):
    items: list[VoiceTestQuestionOut]


class UpdateVoiceTestQuestionIn(CamelModel):
    reworded_text: str | None = Field(default=None, min_length=1)
    spoken_text: str | None = Field(default=None, min_length=1)
    status: VoiceTestQuestionStatus | None = None


class SessionReviewOut(CamelModel):
    """A finished session whose grading failed - no score was recorded, the
    teacher reads the transcript instead."""

    session_id: str
    student_id: str
    student_name: str
    finished_at: datetime
    grading_error: str
    transcript: list[dict]
    answers: list[dict]


class SessionReviewListOut(CamelModel):
    items: list[SessionReviewOut]
