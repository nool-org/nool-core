import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin, TimestampMixin
from .voice_test import AssignmentTargetMode


class HomeworkStatus(str, Enum):
    """GENERATING -> REVIEW -> ASSIGNED -> IN_PROGRESS -> COMPLETED, or
    FAILED off GENERATING.
    """

    GENERATING = "GENERATING"
    REVIEW = "REVIEW"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class HomeworkDifficulty(str, Enum):
    EASY = "EASY"
    MIXED = "MIXED"
    HARD = "HARD"


class StudentHomeworkStatus(str, Enum):
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class Homework(IdMixin, TimestampMixin, Base):
    __tablename__ = "homework"

    test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_tests.id"))
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("classes.id"))
    gap_topic: Mapped[str] = mapped_column(Text)
    gap_mastery_percent: Mapped[int] = mapped_column(Integer)
    total_questions: Mapped[int] = mapped_column(Integer)
    difficulty: Mapped[HomeworkDifficulty] = mapped_column(
        SAEnum(HomeworkDifficulty, name="homework_difficulty")
    )
    target_mode: Mapped[AssignmentTargetMode] = mapped_column(
        SAEnum(AssignmentTargetMode, name="assignment_target_mode")
    )
    completion_window_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[HomeworkStatus] = mapped_column(
        SAEnum(HomeworkStatus, name="homework_status"), default=HomeworkStatus.GENERATING
    )
    assigned_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)

    # Real, LLM-generated "why this Homework, and how to close the gap" -
    # see src/services/homework_insights.py. Nullable/lazily filled on the
    # first GET /me/homework/{id}/context (see student_homework.py) rather
    # than at creation time, so a slow/failed LLM call never blocks
    # homework_autogen.py's auto-generation transaction. insight_generated_at
    # is the cache marker: null means "not generated yet, fall back to a
    # generic placeholder and try again next request."
    insight_what_needs_understanding: Mapped[str | None] = mapped_column(Text, nullable=True)
    insight_references: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    insight_key_idea_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    insight_key_idea_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    insight_connection_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    insight_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set while POST /homework/{id}/generate is building questions - a second
    # call (double click, client retry after a timeout) gets 409 instead of
    # doubling the questions. Stale after GENERATION_LOCK_MINUTES (crash).
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HomeworkDataset(Base):
    __tablename__ = "homework_datasets"
    __table_args__ = (UniqueConstraint("homework_id", "dataset_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    homework_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("homework.id"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("datasets.id"))


class HomeworkBloomDistribution(Base):
    __tablename__ = "homework_bloom_distribution"
    __table_args__ = (UniqueConstraint("homework_id", "bloom_level"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    homework_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("homework.id"))
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    value: Mapped[int] = mapped_column(Integer)


class HomeworkTargetStudent(Base):
    """Only populated when target_mode is SPECIFIC_STUDENTS."""

    __tablename__ = "homework_target_students"
    __table_args__ = (UniqueConstraint("homework_id", "student_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    homework_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("homework.id"))
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class HomeworkQuestion(IdMixin, Base):
    __tablename__ = "homework_questions"
    # don't RETURN is_boss's server default on INSERT (see is_boss below)
    __mapper_args__ = {"eager_defaults": False}

    homework_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("homework.id"))
    order: Mapped[int] = mapped_column(Integer)
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("datasets.id"))
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    # The question-bank row this was taken from (services/homework_builder.py);
    # NULL for a generated question. Lets later homework skip questions the
    # class has already had.
    source_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_questions.id", ondelete="SET NULL"), nullable=True
    )
    # Auto homework's "boss": the concept of the student's lowest-scoring
    # voice-test answer, worth double XP. Deferred + server default so code
    # that never touches it doesn't select or insert it.
    is_boss: Mapped[bool] = mapped_column(Boolean, server_default=false(), deferred=True)


class HomeworkAnswer(IdMixin, TimestampMixin, Base):
    """A student's own answer to one homework question (POST
    /me/homework/{id}/questions/{qid}/answer). One row per (question,
    student): a repeat updates it; XP is awarded only when it's created."""

    __tablename__ = "homework_answers"
    __table_args__ = (UniqueConstraint("homework_question_id", "student_id"),)

    homework_question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("homework_questions.id", ondelete="CASCADE")
    )
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_rating: Mapped[str] = mapped_column(String)  # "got_it" | "confused"
    xp_awarded: Mapped[int] = mapped_column(Integer)


class StudentHomeworkProgress(IdMixin, Base):
    """Per-student assignment/completion state - backs the teacher's
    assignedCount/completedCount rollups and the student-facing
    StudentHomeworkOverview (GET /api/v1/me/homework/current).
    """

    __tablename__ = "student_homework_progress"
    __table_args__ = (UniqueConstraint("homework_id", "student_id"),)

    homework_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("homework.id"))
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    status: Mapped[StudentHomeworkStatus] = mapped_column(
        SAEnum(StudentHomeworkStatus, name="student_homework_status"),
        default=StudentHomeworkStatus.ASSIGNED,
    )
    qa_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired: Mapped[bool] = mapped_column(Boolean, default=False)
