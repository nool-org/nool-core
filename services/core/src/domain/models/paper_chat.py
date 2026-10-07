import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin, TimestampMixin


class PaperChatMessage(IdMixin, TimestampMixin, Base):
    """One Paper Copilot chat turn on a question paper: the teacher's message
    (typed, transcribed or a quick action) or the copilot's reply with the
    proposals it made (C12 proposal dicts, camelCase). Proposals are only
    suggestions - accepting one goes through /actions, never through here."""

    __tablename__ = "paper_chat_messages"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String)  # "teacher" | "copilot"
    text: Mapped[str] = mapped_column(Text)
    proposals: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
