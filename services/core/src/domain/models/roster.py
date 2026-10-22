import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .foundation import UserStatus
from .mixins import IdMixin, TimestampMixin


class SchoolGrade(IdMixin, TimestampMixin, Base):
    """The 'Class' level of the School -> Class -> Section -> Students
    hierarchy - e.g. "Class 10" at one school, holding every section
    (SchoolClass row, below) at that grade. Added additively alongside
    SchoolClass rather than renaming it: SchoolClass/`classes` predates
    this, is what nool-apps (mobile) already calls a "class," and keeps
    that exact name/shape so mobile - not audited alongside this change -
    keeps working unmodified. See migration 6f2f4d9c1a3e_school_grades for
    the backfill of one row per pre-existing (school_id, grade) pair.
    """

    __tablename__ = "school_grades"
    __table_args__ = (UniqueConstraint("school_id", "grade"),)

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"))
    grade: Mapped[int] = mapped_column(Integer)
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status"), default=UserStatus.ACTIVE
    )


class SchoolClass(IdMixin, TimestampMixin, Base):
    __tablename__ = "classes"
    __table_args__ = (UniqueConstraint("school_id", "grade", "section"),)

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"))
    grade: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String)
    # Nullable only because pre-migration rows are backfilled by the
    # migration itself, not left permanently null - every row written by
    # application code (see roster_repository.get_or_create_grade) always
    # sets this. Kept as a plain FK rather than folding SchoolGrade's
    # `grade` int into a join everywhere, since most existing call sites
    # only ever needed SchoolClass.grade directly (unchanged, still here).
    grade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("school_grades.id"), nullable=True
    )
    # Reuses the same PENDING/ACTIVE/DEACTIVATED enum as User.status (a
    # class is only ever ACTIVE or DEACTIVATED in practice, but a second
    # near-identical Postgres enum type for one unused value isn't worth
    # it) - see PATCH /school/classes/{id}/status.
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status"), default=UserStatus.ACTIVE
    )


class TeacherClassAssignment(IdMixin, Base):
    """Which teacher teaches which subject to which class - backs both a
    Teacher's implicit class scoping and School Admin's
    PUT /api/v1/school/classes/{classId}/assignments.
    """

    __tablename__ = "teacher_class_assignments"
    __table_args__ = (UniqueConstraint("teacher_id", "class_id", "subject_id"),)

    teacher_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("classes.id"))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subjects.id"))
    # one branch of Science (Chapter.branch) when 3 teachers share it; NULL = every chapter
    branch: Mapped[str | None] = mapped_column(String, nullable=True)


class StudentProfile(Base):
    """The school-specific half of a STUDENT user - roll number and class
    membership. One-to-one with users (role=STUDENT); kept separate so
    `users` stays role-agnostic.
    """

    __tablename__ = "student_profiles"
    __table_args__ = (UniqueConstraint("class_id", "roll_number"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("classes.id"))
    roll_number: Mapped[int] = mapped_column(Integer)
    guardian_name: Mapped[str | None] = mapped_column(String, nullable=True)
    guardian_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
