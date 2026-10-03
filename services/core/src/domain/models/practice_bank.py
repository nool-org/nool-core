import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin


class PracticeBankEntry(IdMixin, Base):
    """One question archived into a student's personal, cross-Homework
    "Practice Bank" - the moment they confirm that Homework's Q&A complete
    (see confirm_completion in student_homework.py, and
    src/services/practice_bank.py's archive_homework_questions - the one
    write site).

    Not "Question Bank" - that term is already reserved (see
    spec/CLAUDE.md's Terminology list, src/domain/models/dataset.py's
    Dataset/DatasetQuestion, and school_oversight.py's
    SchoolQuestionBankEntryOut) for the teacher/admin-side catalog used to
    *generate* Homework/Question Papers. This is the opposite direction: a
    single student's own kept-for-later set, scoped to just that student.

    Deliberately a full standalone copy of the source HomeworkQuestion's
    text/answer/bloom_level rather than a live pointer to it: the source
    Homework can later be edited/replaced by a teacher (see the question
    PATCH/replace routes in homework.py), and the archived copy must stand
    on its own, unaffected by anything that happens to the source
    afterwards.

    subject_id/chapter_id/topic_id are denormalized from the source
    Homework's VoiceTest at archive time, purely so GET /me/practice-bank
    can filter by subject/chapter without joining through Homework -> a
    (possibly since-changed) VoiceTest for every read.

    Uniqueness on (student_id, source_question_id) is what makes the
    archiving write idempotent when confirm_completion is called more than
    once for the same student+homework (see that route's own docstring on
    replay) - a repeat insert for an already-archived question is a no-op.
    """

    __tablename__ = "practice_bank_entries"
    __table_args__ = (UniqueConstraint("student_id", "source_question_id"),)

    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    # Traceability only - never shown as a live link, since the source
    # Homework may no longer be relevant to the student by the time they
    # revisit this entry.
    source_homework_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("homework.id"))
    source_question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("homework_questions.id")
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=True
    )
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=True
    )
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True
    )
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    order: Mapped[int] = mapped_column(Integer)  # preserves position within its source Homework
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
