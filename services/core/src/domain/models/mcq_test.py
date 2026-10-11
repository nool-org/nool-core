import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, false, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin, TimestampMixin


class McqTest(IdMixin, TimestampMixin, Base):
    """A student's 10-question follow-up MCQ test for one voice test result -
    see services/mcq_test.py. Status is derived: completed_at set =
    completed, unlocked_at set = available, else locked until unlocks_at
    (or until the student confirms that test's homework)."""

    __tablename__ = "mcq_tests"
    __table_args__ = (UniqueConstraint("student_id", "voice_test_id"),)

    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    voice_test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_tests.id"))
    unlocks_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    score_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [{questionId, optionIndex}] as submitted
    answers: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # {before, after, perBloom: {LEVEL: percent}, growth: [{key, kind, before, afterCorrected, delta}]}
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    lifelines_used: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")  # fifty_fifty, ask_milo
    speed_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # one retry of the misses, for practice XP - never changes score_percent / mastery
    retry_score_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McqTestQuestion(IdMixin, Base):
    __tablename__ = "mcq_test_questions"
    __table_args__ = (UniqueConstraint("mcq_test_id", "order"),)

    mcq_test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mcq_tests.id", ondelete="CASCADE"))
    order: Mapped[int] = mapped_column(Integer)
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    concept: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSONB)
    correct_index: Mapped[int] = mapped_column(Integer)
    # NULL = generated
    source_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_questions.id", ondelete="SET NULL"), nullable=True
    )
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)  # one line: why the answer is right
    hint: Mapped[str | None] = mapped_column(Text, nullable=True)  # "Ask Milo" lifeline - never the answer
    is_boss: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # harder, double XP
    gap_concept: Mapped[str | None] = mapped_column(Text, nullable=True)  # the voice-test gap it targets


class McqQuestionAnswer(IdMixin, Base):
    """A student's FIRST answer to one MCQ question (instant feedback). The
    attempt is scored from these; a retry of the misses never overwrites them."""

    __tablename__ = "mcq_question_answers"
    __table_args__ = (UniqueConstraint("mcq_test_question_id", "student_id",
                                       name="uq_mcq_question_answers_question_student"),)

    mcq_test_question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcq_test_questions.id", ondelete="CASCADE"))
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    option_index: Mapped[int] = mapped_column(Integer)
    correct: Mapped[bool] = mapped_column(Boolean)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
