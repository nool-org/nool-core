import uuid
from enum import Enum

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from ..base import Base
from .bloom import BloomLevel
from .custom_question import QuestionType
from .mixins import IdMixin, TimestampMixin
from .question_paper import PaperDifficultyLevel


class DatasetType(str, Enum):
    """What kind of content a Dataset actually holds - the two mechanisms
    this app has built are otherwise indistinguishable except by
    inspecting which columns happen to be filled in:

    QA - a hand-curated bank of ready-made question/answer pairs
    (DatasetQuestion rows), the same shape as CustomQuestion. This is the
    default, and what every Dataset was before PRIMARY_CONTENT existed.

    PRIMARY_CONTENT - raw ingested source material (a textbook) backing a
    knowledge-graph Curriculum root (board+grade+subject_id all set) - see
    Dataset's docstring on those three columns. Grounds real, on-demand
    LLM generation (services/kg's /question-papers/generate) rather than
    storing questions ahead of time; a PRIMARY_CONTENT dataset's own
    `question_count`/DatasetQuestion rows are therefore not meaningful and
    expected to stay empty.

    Every KG-facing code path (sync_curriculum_from_kg, `_paper_kg_identity`)
    checks this explicitly rather than only inferring it from board/grade
    being set, so a QA dataset can never be accidentally treated as a KG
    root even if those columns end up populated by mistake.
    """

    QA = "QA"
    PRIMARY_CONTENT = "PRIMARY_CONTENT"


class Dataset(IdMixin, Base):
    """A global question-bank catalog used as the grounding pool for
    Homework and Question Paper generation. `subject_id` ties a dataset to
    the one Subject it belongs to (a Subject can have many Datasets - e.g.
    separate "Algebra" and "Geometry" datasets both under Math). Nullable
    so pre-existing datasets created before this column existed, or a
    genuinely cross-subject dataset, aren't forced to pick one.

    `board`/`grade` (also nullable, same reasoning as `subject_id`) name
    which knowledge-graph Curriculum root (see services/kg's
    Curriculum{board, grade, subject} node) this dataset corresponds to -
    a dataset like "10th Science" *is* that KG root, with its chapters/
    topics underneath it, not a separate concept from it. Together with
    `subject_id` these are what lets Question Paper (and, later, Homework)
    generation resolve "which part of the knowledge graph does this
    dataset mean" instead of only ever using the paper's own board/grade -
    see question_paper.py's `_paper_kg_identity`. Only meaningful for a
    `type == PRIMARY_CONTENT` dataset - see DatasetType.

    `question_count` stays as a plain stored column for a dataset that's
    only ever been described manually (no real DatasetQuestion rows yet) -
    see GET /admin/datasets' resolution: real row count wins once any
    DatasetQuestion rows exist, this manual value is the fallback only.

    Who can use a dataset (see repositories/dataset_access.py):
    - `school_id` NULL: a catalog dataset owned by the platform. A school
      gets it only when a Super Admin assigns it (SchoolDataset enabled) -
      schools can see what they're assigned but never assign themselves.
    - `school_id` set: that school's own question bank, created by its
      School Admin. Only that school's users can use it; a Super Admin can
      view it, but it can never be assigned to another school.

    `restricted` predates assignment-only access and no longer changes
    visibility (every catalog dataset now needs an assignment); kept for
    API compatibility.
    """

    __tablename__ = "datasets"

    name: Mapped[str] = mapped_column(String)
    question_count: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    type: Mapped[DatasetType] = mapped_column(
        SAEnum(DatasetType, name="dataset_type"), default=DatasetType.QA
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=True)
    board: Mapped[str | None] = mapped_column(String, nullable=True)
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schools.id"), nullable=True, index=True
    )


class DatasetQuestion(IdMixin, TimestampMixin, Base):
    """One real question in a Dataset's own question bank - the actual
    content GET /admin/datasets previously only described via a manually-
    typed `question_count` integer (see requirements.md's flagged gap:
    "no linkage between a Dataset row and actual generated questions
    anywhere in the schema"). Same shape as CustomQuestion (School Admin's
    own per-school content) but scoped to a Dataset instead of a school,
    since this is Super-Admin-owned shared catalog content, not one
    school's private bank. chapter_id/topic_id are optional refinement
    within the Dataset's own subject, not required the way CustomQuestion
    requires chapter_id - a curator can add a question to the bank without
    committing to exact placement yet.
    """

    __tablename__ = "dataset_questions"

    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("datasets.id"))
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=True)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    # Finer placement under topic_id - set by scripts/link_questions_to_curriculum.py
    # (NULL when the question sits in a topic's own intro text, not a subtopic).
    subtopic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subtopics.id"), nullable=True
    )
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    question_type: Mapped[QuestionType] = mapped_column(SAEnum(QuestionType, name="question_type"))
    text: Mapped[str] = mapped_column(Text)
    # Only meaningful for MCQ/TRUE_FALSE - see CustomQuestion's identical
    # column for the same reasoning (validated in the schema layer, not
    # here - see CreateDatasetQuestionIn).
    options: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    answer: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    # Bulk-imported metadata (scripts/import_dataset.py) - all nullable so
    # hand-authored rows through the admin CRUD above stay valid as-is.
    # `external_uid` is the source bank's own stable question id: the same
    # question can sit in several Datasets (e.g. both "Previous Year" and
    # "Important"), one row per Dataset, and generation de-duplicates on it.
    external_uid: Mapped[str | None] = mapped_column(String, nullable=True)
    # Finer than question_type (mcq, very-short, short, long,
    # assertion-reason, case-based, numerical) - question_type stays one of
    # the four values every client already knows.
    format: Mapped[str | None] = mapped_column(String, nullable=True)
    marks: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[PaperDifficultyLevel | None] = mapped_column(
        SAEnum(PaperDifficultyLevel, name="paper_difficulty_level", create_type=False), nullable=True
    )
    solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    concepts: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # Raw subtopic name from the source bank - not yet matched to a
    # Topic/Subtopic row (only chapter 1 is synced from the KG so far).
    subtopic_label: Mapped[str | None] = mapped_column(String, nullable=True)
    years_asked: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    # [{blob, role: question|option|solution, option, caption, kind}] -
    # `blob` is a path inside the question-assets container, never a full
    # URL, so moving storage accounts is config-only (ASSET_BASE_URL).
    # Text refers to them inline as [figure:N] (index into this list).
    figures: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # Provenance: {sources: [{dataset, file, page, extracted_by}], answer_confidence}.
    source: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("dataset_id", "external_uid", name="uq_dataset_questions_dataset_id_external_uid"),
    )


class Vector(UserDefinedType):
    """pgvector's `vector(n)` column - declared here only so the schema
    (and Alembic autogenerate) knows the table. Reading/writing vectors is
    the embedding pipeline's job (scripts/embed_questions.py, later the ai
    service), not core's, so no Python-side (de)serialisation.
    """

    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim

    def get_col_spec(self, **kw) -> str:
        return f"vector({self.dim})"


class QuestionEmbedding(TimestampMixin, Base):
    """One embedding per DatasetQuestion row, for similarity retrieval
    (homework / question paper generation). `model` + `content_hash` let
    the pipeline re-embed only rows whose text or model changed.
    """

    __tablename__ = "question_embeddings"
    __table_args__ = (
        Index(
            "ix_question_embeddings_embedding_hnsw", "embedding",
            postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    dataset_question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_questions.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))


class SchoolDataset(IdMixin, Base):
    """Which datasets a school has enabled - see PUT /api/v1/school/datasets.
    Scopes what a Teacher at that school sees in GET /api/v1/datasets, the
    same way SchoolCurriculum scopes GET /api/v1/subjects.
    """

    __tablename__ = "school_datasets"
    __table_args__ = (UniqueConstraint("school_id", "dataset_id"),)

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("datasets.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
