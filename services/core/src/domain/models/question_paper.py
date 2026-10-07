import uuid
from enum import Enum

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .custom_question import QuestionType
from .mixins import IdMixin, TimestampMixin


class QuestionPaperStatus(str, Enum):
    """DRAFT -> GENERATING -> REVIEW -> FINALIZED, or VALIDATION_FAILED
    off GENERATING. Independent of the Test/Homework learning loop - a
    QuestionPaper never references a VoiceTest.
    """

    DRAFT = "DRAFT"
    GENERATING = "GENERATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REVIEW = "REVIEW"
    FINALIZED = "FINALIZED"


class PaperDifficultyLevel(str, Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class QuestionPaper(IdMixin, TimestampMixin, Base):
    __tablename__ = "question_papers"

    # Not in the reference doc's response shape (it's derived from the
    # caller's token, same as every other Teacher resource) - needed for
    # tenant isolation, since nothing else on this table scopes it to a
    # school.
    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String)
    exam_type: Mapped[str] = mapped_column(String)
    board: Mapped[str] = mapped_column(String)
    grade: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    total_marks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Flat lists of free text, not foreign-keyed relations - ARRAY is the
    # normalized-enough shape here, a join table would just be noise.
    question_types: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    instructions: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # PaperRules is a fixed-shape embedded value object - flattened into
    # columns rather than a separate one-row-per-paper join table.
    allow_internal_choice: Mapped[bool] = mapped_column(Boolean, default=True)
    avoid_duplicate_concepts: Mapped[bool] = mapped_column(Boolean, default=True)
    respect_chapter_weightage: Mapped[bool] = mapped_column(Boolean, default=True)
    avoid_recent_repetition: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[QuestionPaperStatus] = mapped_column(
        SAEnum(QuestionPaperStatus, name="question_paper_status"), default=QuestionPaperStatus.DRAFT
    )
    custom_header_text: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_data_uri: Mapped[str | None] = mapped_column(Text, nullable=True)


class QuestionPaperDatasetShare(Base):
    __tablename__ = "question_paper_dataset_shares"
    __table_args__ = (UniqueConstraint("paper_id", "dataset_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("datasets.id"))
    percent: Mapped[int] = mapped_column(Integer)


class QuestionPaperChapter(Base):
    __tablename__ = "question_paper_chapters"
    __table_args__ = (UniqueConstraint("paper_id", "chapter_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"))


class QuestionPaperTopic(Base):
    __tablename__ = "question_paper_topics"
    __table_args__ = (UniqueConstraint("paper_id", "topic_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"))


class QuestionPaperSection(IdMixin, Base):
    __tablename__ = "question_paper_sections"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    label: Mapped[str] = mapped_column(String)
    question_count: Mapped[int] = mapped_column(Integer)
    marks_each: Mapped[int] = mapped_column(Integer)


class QuestionPaperBloomDistribution(Base):
    __tablename__ = "question_paper_bloom_distribution"
    __table_args__ = (UniqueConstraint("paper_id", "bloom_level"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    value: Mapped[int] = mapped_column(Integer)


class QuestionPaperDifficultyDistribution(Base):
    __tablename__ = "question_paper_difficulty_distribution"
    __table_args__ = (UniqueConstraint("paper_id", "level"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    level: Mapped[PaperDifficultyLevel] = mapped_column(
        SAEnum(PaperDifficultyLevel, name="paper_difficulty_level")
    )
    value: Mapped[int] = mapped_column(Integer)


class QuestionPaperValidation(Base):
    """1:1 with QuestionPaper - present only once GENERATING has run."""

    __tablename__ = "question_paper_validation"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id"), primary_key=True
    )
    coverage_percent: Mapped[int] = mapped_column(Integer)
    marks_accounted_for: Mapped[int] = mapped_column(Integer)
    duplicate_count: Mapped[int] = mapped_column(Integer)
    quality_percent: Mapped[int] = mapped_column(Integer)
    issues: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)


class QuestionSource(str, Enum):
    BANK = "bank"
    AI = "ai"
    EDITED = "edited"  # a teacher edit of a bank/ai question


# Paper Copilot columns (migration c1f5a9e3b7d2) are deferred into one group:
# code that never touches them neither selects nor inserts them. Read them
# with .options(undefer_group(COPILOT_COLUMNS)).
COPILOT_COLUMNS = "paper_copilot"


def _copilot(*args, server_default=sql_text("NULL"), **kwargs):
    # a server default (even NULL) keeps unset columns out of the ORM's INSERT
    return mapped_column(*args, nullable=True, deferred=True, deferred_group=COPILOT_COLUMNS,
                         server_default=server_default, **kwargs)


class QuestionPaperQuestion(IdMixin, Base):
    __tablename__ = "question_paper_questions"
    # don't RETURN source's server default on INSERT (see COPILOT_COLUMNS)
    __mapper_args__ = {"eager_defaults": False}

    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id")
    )
    order: Mapped[int] = mapped_column(Integer)
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    marks: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    question_type: Mapped[QuestionType | None] = _copilot(SAEnum(QuestionType, name="question_type", create_type=False))
    difficulty: Mapped[PaperDifficultyLevel | None] = _copilot(
        SAEnum(PaperDifficultyLevel, name="paper_difficulty_level", create_type=False)
    )
    chapter_id: Mapped[uuid.UUID | None] = _copilot(UUID(as_uuid=True), ForeignKey("chapters.id"))
    topic_id: Mapped[uuid.UUID | None] = _copilot(UUID(as_uuid=True), ForeignKey("topics.id"))
    # The blueprint section (QuestionPaperSection.label) this question fills.
    section_name: Mapped[str | None] = _copilot(String)
    options: Mapped[list[str] | None] = _copilot(JSONB)
    answer: Mapped[str | None] = _copilot(Text)
    source: Mapped[str | None] = _copilot(String, server_default=sql_text("'ai'"))  # QuestionSource
    dataset_question_id: Mapped[uuid.UUID | None] = _copilot(
        UUID(as_uuid=True), ForeignKey("dataset_questions.id", ondelete="SET NULL")
    )


class QuestionPaperVersion(IdMixin, TimestampMixin, Base):
    """One saved state of a paper's questions: version 1 on generate, then
    one per /actions call, text edit or restore (restore = a NEW version
    equal to an older snapshot). `snapshot` is the ordered question list,
    every column (see services/paper_actions.py's snapshot())."""

    __tablename__ = "question_paper_versions"
    __table_args__ = (UniqueConstraint("paper_id", "number"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_papers.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String)  # generate|chat|quick_action|manual|explain|edit|restore
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    snapshot: Mapped[list[dict]] = mapped_column(JSONB)
