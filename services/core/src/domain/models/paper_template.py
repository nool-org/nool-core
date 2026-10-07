import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin, TimestampMixin


class QuestionPaperTemplate(IdMixin, TimestampMixin, Base):
    """A saved question-paper blueprint. `scope` "personal" = only its
    creator sees it, "school" = every teacher of the school (set by a school
    admin). `blueprint` is the CreateQuestionPaperIn body (minus name) as
    JSON, so new blueprint fields ride along without a migration here (a
    designed template's sections also carry their own question_types); the
    columns beside it are just what the list filters/shows. Built-in
    starter templates are code constants (services/paper_template_starters)."""

    __tablename__ = "question_paper_templates"

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"), index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String, default="personal", server_default="personal")
    name: Mapped[str] = mapped_column(String)
    exam_type: Mapped[str] = mapped_column(String)
    board: Mapped[str] = mapped_column(String)
    grade: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"),
                                                         nullable=True)
    total_marks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blueprint: Mapped[dict] = mapped_column(JSONB)
