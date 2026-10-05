import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin, TimestampMixin


class AiAssessorSession(IdMixin, TimestampMixin, Base):
    """One live AI Assessor voice conversation tied to a VoiceTest - a
    realtime (WebRTC) call through colearner, see api/routes/ai_assessor.py.
    """

    __tablename__ = "ai_assessor_sessions"

    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    test_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_tests.id"), nullable=True
    )
    context_label: Mapped[str] = mapped_column(String)
    duration_seconds: Mapped[int] = mapped_column(Integer)


class AiAssessorSessionBloomLevel(Base):
    __tablename__ = "ai_assessor_session_bloom_levels"
    __table_args__ = (UniqueConstraint("session_id", "bloom_level"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_assessor_sessions.id")
    )
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))


class AiAssessorSessionRecord(TimestampMixin, Base):
    """What a finished session actually produced, kept as-is for review and
    re-grading: colearner's transcript, per-question answers and raw grader
    report. needs_review = grading failed, so no score was recorded."""

    __tablename__ = "ai_assessor_session_records"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_assessor_sessions.id"), primary_key=True
    )
    transcript: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    answers: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
