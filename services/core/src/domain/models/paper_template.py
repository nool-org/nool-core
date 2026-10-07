import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin, TimestampMixin


class QuestionPaperTemplate(IdMixin, TimestampMixin, Base):
    """A saved question-paper blueprint, shared with every teacher of the
    school. `blueprint` is the CreateQuestionPaperIn body (minus name) as
    JSON, so new blueprint fields ride along without a migration here; the
    columns beside it are just what the list shows."""

    __tablename__ = "question_paper_templates"

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"), index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String)
    exam_type: Mapped[str] = mapped_column(String)
    board: Mapped[str] = mapped_column(String)
    grade: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    total_marks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blueprint: Mapped[dict] = mapped_column(JSONB)
