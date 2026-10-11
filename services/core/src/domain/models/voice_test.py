import uuid
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, false, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin, TimestampMixin


class TestStatus(str, Enum):
    """DRAFT -> SCHEDULED -> ACTIVE -> COMPLETED -> RESULTS_PROCESSING ->
    RESULTS_READY. Each is a real, distinct state - never collapsed.
    """

    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    RESULTS_PROCESSING = "RESULTS_PROCESSING"
    RESULTS_READY = "RESULTS_READY"


class AssignmentTargetMode(str, Enum):
    WHOLE_CLASS = "WHOLE_CLASS"
    SPECIFIC_STUDENTS = "SPECIFIC_STUDENTS"


class VoiceTestPrepStatus(str, Enum):
    """Question prep (services/voice_test_prep.py): PENDING while it runs -
    no session may open yet; READY or FAILED (the co-learner improvises
    from textbook_context) once done."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class VoiceTest(IdMixin, TimestampMixin, Base):
    __tablename__ = "voice_tests"

    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("classes.id"))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True
    )
    duration_minutes: Mapped[int] = mapped_column(Integer)
    completion_window_hours: Mapped[int] = mapped_column(Integer)
    target_mode: Mapped[AssignmentTargetMode] = mapped_column(
        SAEnum(AssignmentTargetMode, name="assignment_target_mode")
    )
    status: Mapped[TestStatus] = mapped_column(
        SAEnum(TestStatus, name="test_status"), default=TestStatus.DRAFT
    )
    assigned_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    # Both NULL until generation succeeds (see voice_test.py's create_test) -
    # a KG/LLM failure at creation time never blocks the test itself, it
    # just leaves these unset. When unset, the AI Assessor session falls
    # back to colearner's own hardcoded generic defaults (see
    # services/colearner/src/services/chained_service.py's DEFAULT_CONTEXT)
    # rather than failing the session.
    reference_questions: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    textbook_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    prep_status: Mapped[VoiceTestPrepStatus] = mapped_column(
        SAEnum(VoiceTestPrepStatus, name="voice_test_prep_status"), default=VoiceTestPrepStatus.READY
    )
    # When the teacher scheduled it; students have completion_window_hours from here
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def due_at(self) -> datetime | None:
        return self.scheduled_at + timedelta(hours=self.completion_window_hours) if self.scheduled_at else None

    @classmethod
    def still_open(cls):
        """SQL filter: not past its deadline (never-scheduled rows have none)."""
        return (cls.scheduled_at.is_(None)) | (
            cls.scheduled_at + func.make_interval(0, 0, 0, 0, cls.completion_window_hours) > func.now())


class VoiceTestBloomLevel(Base):
    __tablename__ = "voice_test_bloom_levels"
    __table_args__ = (UniqueConstraint("test_id", "bloom_level"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_tests.id"))
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))


class VoiceTestTargetStudent(Base):
    """The real per-student delivery list for every Test, regardless of
    target_mode - every read path a student's own app uses to find "my
    assigned tests" joins through this table. For SPECIFIC_STUDENTS these
    rows come straight from the teacher's picked list; for WHOLE_CLASS
    they're resolved from the class roster once, at creation time (see
    voice_test.py's create_test) - either way, this table is always the
    complete, queryable answer to "who was this test actually sent to."
    """

    __tablename__ = "voice_test_target_students"
    __table_args__ = (UniqueConstraint("test_id", "student_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_tests.id"))
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class VoiceTestQuestionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class VoiceTestQuestion(IdMixin, TimestampMixin, Base):
    """One of the questions every student of a Test is asked, in `order` -
    picked once per Test from the school's datasets and reworded for speech
    by services/voice_test_prep.py. The teacher may edit/approve/swap them
    (voice_test.py's /questions routes). dataset_question_id is NULL only if
    the source question was later deleted."""

    __tablename__ = "voice_test_questions"
    __table_args__ = (UniqueConstraint("voice_test_id", "order"),)

    voice_test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_tests.id"))
    order: Mapped[int] = mapped_column(Integer)
    dataset_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_questions.id", ondelete="SET NULL"), nullable=True
    )
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    subtopic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subtopics.id"), nullable=True
    )
    concepts: Mapped[list[str]] = mapped_column(JSONB, default=list)
    original_text: Mapped[str] = mapped_column(Text)
    reworded_text: Mapped[str] = mapped_column(Text)
    # reworded_text with formulas/symbols written out in words, for TTS.
    spoken_text: Mapped[str] = mapped_column(Text)
    # The grading rubric - empty when generation was unavailable.
    expected_points: Mapped[list[str]] = mapped_column(JSONB, default=list)
    misconceptions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    hints: Mapped[list[str]] = mapped_column(JSONB, default=list)  # gentle, then stronger
    # adaptive difficulty: the same slot asked more gently, for a student who is struggling -
    # {"spoken_text", "expected_points", "misconceptions", "hints"} or NULL
    easier_version: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # quality loop across students: how often asked, how often it confused (repeat requests / no answer)
    asked_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confused_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    needs_rewording: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    status: Mapped[VoiceTestQuestionStatus] = mapped_column(
        SAEnum(VoiceTestQuestionStatus, name="voice_test_question_status"), default=VoiceTestQuestionStatus.DRAFT
    )
