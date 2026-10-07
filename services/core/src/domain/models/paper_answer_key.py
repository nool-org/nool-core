import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class QuestionPaperAnswerKey(Base):
    """1:1 with a QuestionPaperQuestion: its model answer and step marking
    [{point, marks}]. `updated_by` is NULL while the key is AI-written and set
    once a teacher edits it - regeneration leaves those alone unless asked to
    overwrite. Rows go with their question (ON DELETE CASCADE): regenerating a
    paper's questions drops their keys too.
    """

    __tablename__ = "question_paper_answer_keys"

    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_paper_questions.id", ondelete="CASCADE"), primary_key=True
    )
    answer: Mapped[str] = mapped_column(Text)
    marking_points: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class QuestionPaperParallel(Base):
    """A Set B paper -> the paper it parallels."""

    __tablename__ = "question_paper_parallels"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id", ondelete="CASCADE"), primary_key=True
    )
    parallel_of: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
