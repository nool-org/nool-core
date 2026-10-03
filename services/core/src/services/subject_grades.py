"""Enforces that a Subject only gets newly assigned to a school's
curriculum (SchoolCurriculum) or a Class (GradeSubject) once a Super Admin
has declared which grades it applies to - see SubjectGrade's model
docstring. A SchoolCurriculum/GradeSubject row written before this
constraint existed keeps working regardless of its subject's current
grade links - only a brand-new grant is checked.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ValidationError

from src.domain.models import GradeSubject, SchoolCurriculum, Subject, SubjectGrade


async def grades_by_subject(session: AsyncSession, subject_ids: list) -> dict[str, list[int]]:
    """Maps subject_id (str) -> sorted list of its linked grades, for
    hydrating SubjectOut/SubjectToggle's `grades` field. Subjects with no
    rows are simply absent from the returned dict."""
    if not subject_ids:
        return {}
    result = await session.execute(select(SubjectGrade).where(SubjectGrade.subject_id.in_(subject_ids)))
    by_subject: dict[str, list[int]] = {}
    for row in result.scalars().all():
        by_subject.setdefault(str(row.subject_id), []).append(row.grade)
    for grades in by_subject.values():
        grades.sort()
    return by_subject


async def picker_subjects_for_grade(session: AsyncSession, grade_id: str, grade: int) -> list[Subject]:
    """Subjects selectable for this Class: linked to its grade, or already
    assigned here from before SubjectGrade existed."""
    result = await session.execute(
        select(Subject)
        .outerjoin(SubjectGrade, SubjectGrade.subject_id == Subject.id)
        .outerjoin(
            GradeSubject,
            (GradeSubject.subject_id == Subject.id) & (GradeSubject.grade_id == grade_id),
        )
        .where((SubjectGrade.grade == grade) | (GradeSubject.id.is_not(None)))
        .distinct()
    )
    return list(result.scalars().all())


async def picker_subjects_for_school(session: AsyncSession, school_id: str) -> list[Subject]:
    """Subjects selectable for a school-wide grant: linked to at least one
    grade, or already assigned to this school from before SubjectGrade
    existed."""
    result = await session.execute(
        select(Subject)
        .outerjoin(SubjectGrade, SubjectGrade.subject_id == Subject.id)
        .outerjoin(
            SchoolCurriculum,
            (SchoolCurriculum.subject_id == Subject.id) & (SchoolCurriculum.school_id == school_id),
        )
        .where(SubjectGrade.id.is_not(None) | SchoolCurriculum.id.is_not(None))
        .distinct()
    )
    return list(result.scalars().all())


async def validate_new_grants_for_grade(session: AsyncSession, subject_ids: set[str], grade: int) -> None:
    if not subject_ids:
        return
    result = await session.execute(
        select(SubjectGrade.subject_id).where(SubjectGrade.grade == grade, SubjectGrade.subject_id.in_(subject_ids))
    )
    eligible = {str(row[0]) for row in result.all()}
    invalid = subject_ids - eligible
    if invalid:
        raise ValidationError(
            f"These subjects aren't linked to grade {grade} yet - link them to a grade from the "
            f"global catalog first: {', '.join(sorted(invalid))}"
        )


async def validate_new_grants_have_any_grade(session: AsyncSession, subject_ids: set[str]) -> None:
    if not subject_ids:
        return
    result = await session.execute(
        select(SubjectGrade.subject_id).where(SubjectGrade.subject_id.in_(subject_ids)).distinct()
    )
    eligible = {str(row[0]) for row in result.all()}
    invalid = subject_ids - eligible
    if invalid:
        raise ValidationError(
            f"These subjects aren't linked to any grade yet - link them from the global catalog "
            f"first: {', '.join(sorted(invalid))}"
        )


async def set_subject_grades(session: AsyncSession, subject_id, grades: set[int]) -> None:
    """Full replace of one Subject's grade links - same "PUT replaces the
    whole set" convention as GradeSubject/SchoolCurriculum."""
    existing = await session.execute(select(SubjectGrade).where(SubjectGrade.subject_id == subject_id))
    by_grade = {row.grade: row for row in existing.scalars().all()}
    for grade, row in by_grade.items():
        if grade not in grades:
            await session.delete(row)
    for grade in grades - set(by_grade.keys()):
        session.add(SubjectGrade(subject_id=subject_id, grade=grade))


async def add_subject_grades(session: AsyncSession, subject_id, grades: set[int]) -> None:
    """Union `grades` onto whatever a Subject already has - used when
    reusing an existing (possibly still ungraded) Subject by name rather
    than replacing its links outright."""
    existing = await session.execute(select(SubjectGrade.grade).where(SubjectGrade.subject_id == subject_id))
    have = {row[0] for row in existing.all()}
    for grade in grades - have:
        session.add(SubjectGrade(subject_id=subject_id, grade=grade))


async def ensure_subjects_enabled_for_school(session: AsyncSession, subject_ids, school_id) -> None:
    """Only a Super Admin gives a school its subjects (school_curriculum);
    a school can assign teachers only to subjects it has been given."""
    ids = {str(s) for s in subject_ids if s}
    if not ids:
        return
    enabled = {str(row[0]) for row in (await session.execute(
        select(SchoolCurriculum.subject_id).where(
            SchoolCurriculum.school_id == school_id, SchoolCurriculum.enabled.is_(True),
            SchoolCurriculum.subject_id.in_(ids))
    )).all()}
    if missing := ids - enabled:
        names = [name for (name,) in (await session.execute(select(Subject.name).where(Subject.id.in_(missing)))).all()]
        raise ValidationError(
            f"{', '.join(sorted(names)) or 'That subject'} isn't enabled for this school - "
            "a Super Admin assigns a school's subjects.")


async def ensure_subjects_fit_grade(session: AsyncSession, subject_ids, grade: int, *, what: str) -> None:
    """A subject belongs to one grade (Subject.grade): a grade-9 "Science"
    must never be attached to a grade-10 class, paper or question bank -
    its chapters and topics are grade 9's. Legacy grade-less subjects pass."""
    ids = {str(s) for s in subject_ids if s}
    if not ids:
        return
    mismatched = (await session.execute(
        select(Subject.name, Subject.grade).where(
            Subject.id.in_(ids), Subject.grade.is_not(None), Subject.grade != grade)
    )).all()
    if mismatched:
        names = ", ".join(f"{name} (grade {g})" for name, g in mismatched)
        raise ValidationError(f"{names} can't be used for {what} - it belongs to another grade.")


async def ensure_class_grade_change_allowed(session: AsyncSession, school_class, new_grade: int) -> None:
    """A class's subjects, tests and homework are its grade's (grade-N
    chapters/topics). Changing the grade is only allowed while nothing is
    attached yet - otherwise a grade-9 class would keep grade-10 content."""
    if new_grade == school_class.grade:
        return
    from src.domain.models import Homework, TeacherClassAssignment, VoiceTest

    for column, what in ((TeacherClassAssignment.class_id, "teacher assignments"), (VoiceTest.class_id, "tests"),
                         (Homework.class_id, "homework")):
        if (await session.execute(select(column).where(column == school_class.id).limit(1))).first():
            raise ConflictError(f"This class already has {what} for grade {school_class.grade} - "
                                f"create a new grade-{new_grade} class instead of changing this one.")
