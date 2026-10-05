import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint
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
