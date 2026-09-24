import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin


class Subject(IdMixin, Base):
    """A global catalog, not per-school - SchoolCurriculum below controls
    which subjects a given school has enabled.
    """

    __tablename__ = "subjects"

    name: Mapped[str] = mapped_column(String, unique=True)
    # nool-kg names its graphs after the bare subject name it ingested
    # (e.g. "Science" -> Science_Grade_{grade}_NCERT) - a catalog Subject's
    # own display `name` can be anything ("Class 10 Science", "Physical
    # Science", ...) and has no reason to match that exactly. NULL (the
    # default) means "use `name` as-is" - only set this when nool-kg's own
    # subject name genuinely differs from the display name. See
    # nool_kg_client.graph_name_for and admin_catalog.py's sync routes.
    #
    # NEVER probe a candidate name against nool-kg's chapters/topics
    # endpoints to "check" it - confirmed live that querying a graph name
    # that doesn't exist yet silently creates an empty one there forever,
    # with no delete endpoint exposed to undo it. Always validate against
    # nool_kg_client.list_subjects()'s own returned names instead.
    kg_subject_name: Mapped[str | None] = mapped_column(String, nullable=True)


class Chapter(IdMixin, Base):
    __tablename__ = "chapters"

    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    name: Mapped[str] = mapped_column(String)
    # Sequence within its subject - the "Journey" chapter map
    # (src/api/routes/journey.py) orders on this to decide which chapter is
    # DONE/CURRENT/LOCKED. Nullable: existing chapters get one via the
    # migration's data backfill (insertion order); a chapter added later
    # without an explicit order sorts after every ordered one.
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which grade this chapter's content belongs to - Subject itself is
    # deliberately grade-agnostic (e.g. one "Science" row spans 9th and
    # 10th), so grade has to live here instead, one level down. NULL means
    # "applies to any grade" (every chapter created before this column
    # existed, or one entered by hand without picking a grade) - GET
    # /subjects/{id}/chapters?grade= treats NULL as always-matching rather
    # than hiding it. Set from the source Dataset's own `grade` by
    # POST /admin/curriculum/sync-from-kg (see admin_catalog.py) - without
    # this, a second-grade's ingested book would silently merge into the
    # same flat chapter list as the first.
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stable match key for POST /admin/curriculum/sync-from-kg (see
    # admin_catalog.py) - the kg-service's Chapter node id. NULL for every
    # chapter created by hand through the ordinary admin CRUD below; only
    # sync-written rows carry one. Unique so re-sync upserts, never
    # duplicates.
    kg_ref: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)


class Topic(IdMixin, Base):
    __tablename__ = "topics"

    chapter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chapters.id"))
    name: Mapped[str] = mapped_column(String)
    # Same role as Chapter.kg_ref above, one level down (the KG's Section id).
    kg_ref: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)


class Subtopic(IdMixin, Base):
    __tablename__ = "subtopics"

    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"))
    name: Mapped[str] = mapped_column(String)
    # Same role as Chapter.kg_ref/Topic.kg_ref, one level down (the KG's
    # Subtopic id).
    kg_ref: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)


class SubjectGrade(IdMixin, Base):
    """Which grades a Subject applies to - a global catalog fact a Super
    Admin declares once per Subject (e.g. "Science" -> grades 9, 10), not a
    per-school assignment. Both SchoolCurriculum (enabling a Subject for a
    school) and GradeSubject (enabling it for one Class) only accept a
    brand-new grant for a Subject that has at least one row here - see
    src/services/subject_grades.py. A Subject with zero rows here simply
    can't be newly assigned anywhere yet; SchoolCurriculum/GradeSubject
    rows written before this table existed are left working regardless.
    """

    __tablename__ = "subject_grades"
    __table_args__ = (UniqueConstraint("subject_id", "grade"),)

    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    grade: Mapped[int] = mapped_column(Integer)


class SchoolCurriculum(IdMixin, Base):
    """Which subjects a school has enabled - see PUT /api/v1/school/curriculum.
    Scopes what a Teacher at that school sees in GET /api/v1/subjects.
    """

    __tablename__ = "school_curriculum"
    __table_args__ = (UniqueConstraint("school_id", "subject_id"),)

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class GradeSubject(IdMixin, Base):
    """Which subjects a Class (SchoolGrade) teaches - narrower than the
    school-wide SchoolCurriculum toggle above. An admin picks this
    explicitly per Class; it is not auto-derived from the school's own
    enabled-subjects list (a school could enable Math school-wide but not
    every grade teaches it in every board's curriculum). See
    PUT /school/grades/{gradeId}/subjects.
    """

    __tablename__ = "grade_subjects"
    __table_args__ = (UniqueConstraint("grade_id", "subject_id"),)

    grade_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("school_grades.id"))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
