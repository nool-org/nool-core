import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, false, func, text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin


class ClassTopicProgress(IdMixin, Base):
    """A topic the teacher has taught in a class - daily practice only asks
    about taught topics."""

    __tablename__ = "class_topic_progress"
    __table_args__ = (UniqueConstraint("class_id", "topic_id", name="uq_class_topic_progress_class_topic"),)

    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("classes.id", ondelete="CASCADE"))
    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"))
    taught_on: Mapped[date] = mapped_column(Date)
    marked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudentTopicMastery(IdMixin, Base):
    """The student's running mastery of one topic (0-100) and when it is next
    due for review (spaced repetition)."""

    __tablename__ = "student_topic_mastery"
    __table_args__ = (UniqueConstraint("student_id", "topic_id", name="uq_student_topic_mastery_student_topic"),)

    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"))
    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"))
    mastery: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    correct: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_practised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class PracticeQuestion(IdMixin, Base):
    """The reusable pool daily practice draws from: bank questions and
    generated ones, shared across students so each is written only once."""

    __tablename__ = "practice_questions"

    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level", create_type=False))
    kind: Mapped[str] = mapped_column(String(20))  # mcq | voice | who_is_right | make_your_own | fix_steps
    text: Mapped[str] = mapped_column(Text)
    # the type's details (services/question_types.py) - characters and reasons, check and fields, steps and fixes
    spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    options: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    correct_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_points: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    misconceptions: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    source_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_questions.id", ondelete="SET NULL"), nullable=True)
    origin: Mapped[str] = mapped_column(String(20))  # bank | generated
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyPracticeSession(IdMixin, Base):
    """One student's practice for one day - or, with a topic, a topic quiz
    (attempt 1) or its re-check (attempt 2): see services/topic_quiz.py."""

    __tablename__ = "daily_practice_sessions"
    __table_args__ = (
        Index("uq_daily_practice_sessions_student_date", "student_id", "practice_date", unique=True,
              postgresql_where=text("topic_id IS NULL")),
        Index("uq_topic_quizzes_student_topic_attempt", "student_id", "topic_id", "attempt", unique=True,
              postgresql_where=text("topic_id IS NOT NULL")),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    practice_date: Mapped[date] = mapped_column(Date)
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open")  # open | completed
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # topic quizzes only
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    attempt: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1 quiz | 2 re-check
    score_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_bloom: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # level -> percent
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)  # homework | mastered | needs_teacher
    homework_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("homework.id", ondelete="SET NULL"), nullable=True)


class DailyPracticeItem(IdMixin, Base):
    """One question in a daily session and the student's answer to it."""

    __tablename__ = "daily_practice_items"
    __table_args__ = (UniqueConstraint("session_id", "order", name="uq_daily_practice_items_session_order"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("daily_practice_sessions.id", ondelete="CASCADE"))
    order: Mapped[int] = mapped_column(Integer)
    practice_question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("practice_questions.id"))
    kind: Mapped[str] = mapped_column(String(20))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level", create_type=False))
    review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # earlier-chapter review
    option_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)  # recorded answers: text only, audio not kept
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feedback: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {"good": str, "add": str}
    # what the student did on a who_is_right / make_your_own / fix_steps question (question_types.py)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
