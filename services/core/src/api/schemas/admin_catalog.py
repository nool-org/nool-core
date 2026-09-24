from datetime import datetime

from pydantic import Field, field_validator, model_validator

from src.domain.models import BloomLevel, DatasetType, QuestionType

from .common import CamelModel

_CHOICE_TYPES = (QuestionType.MCQ, QuestionType.TRUE_FALSE)


class SubjectOut(CamelModel):
    id: str
    name: str
    # Which grades this Subject applies to - see SubjectGrade's docstring.
    # A Subject can't be newly assigned to a school or Class curriculum
    # until this is non-empty.
    grades: list[int] = Field(default_factory=list)
    # None = nool-kg's own subject name is the same as `name`. Set only
    # when the catalog's display name ("Class 10 Science") differs from
    # what's actually in the KG ("Science") - see Subject.kg_subject_name.
    kg_subject_name: str | None = None


class CreateSubjectIn(CamelModel):
    name: str
    # Required, non-empty: a Subject with no grade link can't be assigned
    # to anything yet, so creating one without picking at least one grade
    # would just produce a dead-end catalog row - see SubjectGrade.
    grades: list[int] = Field(min_length=1)
    kg_subject_name: str | None = None

    @field_validator("grades")
    @classmethod
    def _unique_grades(cls, grades: list[int]) -> list[int]:
        return sorted(set(grades))


class UpdateSubjectIn(CamelModel):
    name: str
    # Full replace of this Subject's grade links, same convention as
    # GradeSubject/SchoolCurriculum's PUT endpoints. Required (not
    # optional) for the same reason as CreateSubjectIn - a rename that
    # dropped every grade would silently orphan the subject.
    grades: list[int] = Field(min_length=1)
    kg_subject_name: str | None = None

    @field_validator("grades")
    @classmethod
    def _unique_grades(cls, grades: list[int]) -> list[int]:
        return sorted(set(grades))


class SyncFromKgResultOut(CamelModel):
    dataset_id: str
    dataset_name: str
    subject: str
    board: str
    grade: int
    chapters_synced: int
    topics_synced: int
    subtopics_synced: int = 0


class SyncFromKgOut(CamelModel):
    # One entry per Dataset that names a KG root (board+grade+subject_id
    # all set) - a Dataset like "10th Science" IS that root, so syncing is
    # inherently per-dataset now, not a single hardcoded pull. Empty is a
    # legitimate outcome (no dataset names a KG root yet), not an error.
    results: list[SyncFromKgResultOut]


class SyncFromKgIn(CamelModel):
    # None (the default) = every qualifying PRIMARY_CONTENT dataset, same
    # as before this field existed.
    dataset_id: str | None = None
    # None = include every chapter/topic the KG returns; a non-null list
    # (from the tree-view picker) includes only those refs - lets a Super
    # Admin bring in e.g. just Chapter 1 today and more later without
    # re-syncing what's already there (existing rows outside the selection
    # are simply left untouched, never removed).
    include_chapter_refs: list[str] | None = None
    include_topic_refs: list[str] | None = None
    include_subtopic_refs: list[str] | None = None
    # Client-generated id for the matching /ws/admin/curriculum-sync/{job_id}
    # progress socket the tree-view page opens before POSTing here. None
    # (e.g. a script or test calling this route directly) just skips
    # progress broadcasting.
    job_id: str | None = None


class KgTreeSubtopicOut(CamelModel):
    ref: str
    number: str
    name: str


class KgTreeTopicOut(CamelModel):
    ref: str
    number: str
    name: str
    subtopics: list[KgTreeSubtopicOut] = Field(default_factory=list)


class KgTreeChapterOut(CamelModel):
    ref: str
    number: int
    name: str
    topics: list[KgTreeTopicOut]


class KgTreeOut(CamelModel):
    board: str
    grade: int
    subject: str
    chapters: list[KgTreeChapterOut]


class KgSubjectOut(CamelModel):
    # A real, populated subject/grade combination nool-kg currently has -
    # see nool_kg_client.list_curriculum_subjects. `name` here is exactly
    # what nool-kg calls it - what a Subject's kg_subject_name should be
    # set to (or `name` itself, if they already match) to sync from it.
    name: str
    grade: int
    chapter_count: int


class SyncSubjectFromKgIn(CamelModel):
    # Must be one of the Subject's own SubjectGrade links (see
    # subject_grades.py) - which grade's KG content to pull, since one
    # Subject (e.g. "Science") can span several.
    grade: int
    # None = include every chapter/topic the KG returns; a non-null list
    # (from the tree-view picker) includes only those refs - what's
    # already in Postgres outside the selection is left untouched, never
    # removed, so a Super Admin can bring content in incrementally.
    include_chapter_refs: list[str] | None = None
    include_topic_refs: list[str] | None = None
    include_subtopic_refs: list[str] | None = None
    # Client-generated id for the matching /ws/admin/curriculum-sync/{job_id}
    # progress socket the tree-view page opens before POSTing here.
    job_id: str | None = None
    # Overrides the Subject's own kg_subject_name/name for this one sync,
    # same as get_subject_kg_tree's kgSubjectName query param - lets the
    # KG-subject picker sync from an exact real name without saving it.
    kg_subject_name: str | None = None


class SyncSubjectFromKgOut(CamelModel):
    subject_id: str
    subject: str
    grade: int
    chapters_synced: int
    topics_synced: int
    subtopics_synced: int = 0


class ChapterOut(CamelModel):
    id: str
    subject_id: str
    name: str


class CreateChapterIn(CamelModel):
    name: str


class UpdateChapterIn(CamelModel):
    name: str


class TopicOut(CamelModel):
    id: str
    chapter_id: str
    name: str


class CreateTopicIn(CamelModel):
    name: str


class UpdateTopicIn(CamelModel):
    name: str


class SubtopicOut(CamelModel):
    id: str
    topic_id: str
    name: str


class CreateSubtopicIn(CamelModel):
    name: str


class UpdateSubtopicIn(CamelModel):
    name: str


class DatasetOut(CamelModel):
    id: str
    name: str
    # The real count of DatasetQuestion rows once any exist; falls back to
    # the manually-typed value for a dataset with no real content yet -
    # see Dataset.question_count's docstring.
    question_count: int
    description: str
    subject_id: str | None = None
    # Which kg-service Curriculum root this dataset corresponds to - see
    # Dataset's docstring. Null until a Super Admin sets them.
    board: str | None = None
    grade: int | None = None
    # False (the default catalog) or True (hidden unless a school
    # explicitly enables it) - see Dataset.restricted's docstring.
    restricted: bool = False
    # QA (a hand-curated question/answer bank) or PRIMARY_CONTENT (raw
    # ingested source content backing a KG Curriculum root) - see
    # DatasetType's docstring.
    type: DatasetType = DatasetType.QA


class CreateDatasetIn(CamelModel):
    name: str
    question_count: int
    description: str
    subject_id: str | None = None
    board: str | None = None
    grade: int | None = None
    restricted: bool = False
    type: DatasetType = DatasetType.QA


class UpdateDatasetIn(CamelModel):
    name: str | None = None
    question_count: int | None = None
    description: str | None = None
    subject_id: str | None = None
    board: str | None = None
    grade: int | None = None
    restricted: bool | None = None
    type: DatasetType | None = None


class DatasetQuestionOut(CamelModel):
    id: str
    dataset_id: str
    chapter_id: str | None
    topic_id: str | None
    bloom_level: BloomLevel
    question_type: QuestionType
    text: str
    options: list[str] | None
    answer: str
    created_by: str
    created_at: datetime


def _check_dataset_question_options(question_type: QuestionType, options: list[str] | None, answer: str) -> None:
    if question_type in _CHOICE_TYPES:
        if not options or len(options) < 2:
            raise ValueError(f"{question_type.value} questions need at least 2 options.")
        if answer not in options:
            raise ValueError("answer must exactly match one of the given options.")
    elif options is not None:
        raise ValueError(f"{question_type.value} questions don't take options - leave it blank.")


class CreateDatasetQuestionIn(CamelModel):
    chapter_id: str | None = None
    topic_id: str | None = None
    bloom_level: BloomLevel
    question_type: QuestionType
    text: str
    options: list[str] | None = None
    answer: str

    @model_validator(mode="after")
    def _validate_options(self) -> "CreateDatasetQuestionIn":
        _check_dataset_question_options(self.question_type, self.options, self.answer)
        return self


class UpdateDatasetQuestionIn(CamelModel):
    chapter_id: str | None = None
    topic_id: str | None = None
    bloom_level: BloomLevel | None = None
    question_type: QuestionType | None = None
    text: str | None = None
    options: list[str] | None = None
    answer: str | None = None
