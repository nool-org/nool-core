import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin


class AiUsageEvent(IdMixin, Base):
    """One AI request Core made (a generation/colearner call, or a finished
    live voice test) with its measured cost - written best-effort by
    src/services/ai_usage.py, read by the Super Admin's /admin/ai-usage.
    The school/teacher/student ids deliberately have no FKs: deleting a user
    or school must never lose its cost history."""

    __tablename__ = "ai_usage_events"
    __table_args__ = (
        Index("ix_ai_usage_events_created_at", "created_at"),
        Index("ix_ai_usage_events_school_created", "school_id", "created_at"),
        Index("ix_ai_usage_events_feature_created", "feature", "created_at"),
        Index("ix_ai_usage_events_student_id", "student_id"),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    service: Mapped[str] = mapped_column(String(20))  # generation | colearner
    endpoint: Mapped[str] = mapped_column(Text)
    feature: Mapped[str] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(Text, nullable=True)  # the first/primary model
    models: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    calls: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    inr: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[int] = mapped_column(Integer)  # HTTP status, 0 = network error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    school_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    teacher_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    student_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
