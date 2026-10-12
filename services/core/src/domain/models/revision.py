import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin


class RevisionDeck(IdMixin, Base):
    """A topic's revision cards - generated once (src/services/revision_cards.py)
    and shown to every student; only a teacher/admin regenerate replaces them.
    One row per topic: the unique topic_id is what makes concurrent first
    opens generate exactly once."""

    __tablename__ = "revision_decks"
    __table_args__ = (UniqueConstraint("topic_id", name="uq_revision_decks_topic_id"),)

    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"))
    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"),
                                                  index=True)
    status: Mapped[str] = mapped_column(String(20))  # ready | generating | failed
    # [{kind: concept|definition|formula|mistake|check, title, body, answer}]
    cards: Mapped[list[dict]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    source: Mapped[str] = mapped_column(String(20), default="generated", server_default="generated")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
