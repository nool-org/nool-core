"""The one rule for which Datasets a user can see or build content from -
GET /datasets, Homework create and Question Paper create/update all go
through here instead of re-deriving it per route.

A Dataset is visible when BOTH hold:

1. It belongs to the user's school: either a catalog dataset a Super Admin
   has assigned to the school (school_datasets, enabled), or one of the
   school's own datasets (Dataset.school_id). Nothing else - there is no
   "school not configured -> everything" fallback, and a school's own
   dataset is never visible to another school. A Super Admin sees all.
   And its subject must be enabled for the school (school_curriculum):
   disabling a subject switches off every dataset of that subject - catalog
   and own banks - without touching assignments, so re-enabling restores
   them.
2. Class scope: every usable dataset has `grade` and `subject_id` (e.g.
   CBSE 10 Science) and is only visible to users of that class - a Teacher
   through teacher_class_assignments (grade of the section + subject
   taught), a Student through their own section's grade (any subject).
   School/Super Admins aren't class-scoped. A dataset missing either is
   usable by no school (formerly "visible to everyone").
"""
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ForbiddenError, ValidationError
from src.domain.models import (
    Subject,
    Dataset,
    Role,
    SchoolClass,
    SchoolCurriculum,
    SchoolDataset,
    StudentProfile,
    TeacherClassAssignment,
    User,
)


async def visible_datasets(
    session: AsyncSession,
    user: User,
    *,
    subject_id: str | UUID | None = None,
    grade: int | None = None,
) -> list[Dataset]:
    """Datasets `user` may use, optionally narrowed to one class context
    (e.g. a Homework's test class grade + subject). Narrowing only drops
    class-scoped datasets for other grades/subjects - unscoped ones stay.
    """
    query = select(Dataset)
    if user.role != Role.SUPER_ADMIN:
        subjects_on = await enabled_subject_ids(session, user.school_id)
        query = query.where(or_(Dataset.subject_id.is_(None), Dataset.subject_id.in_(subjects_on)))
        assigned = select(SchoolDataset.dataset_id).where(
            SchoolDataset.school_id == user.school_id, SchoolDataset.enabled.is_(True)
        )
        query = query.where(or_(
            Dataset.school_id == user.school_id,
            and_(Dataset.school_id.is_(None), Dataset.id.in_(assigned)),
        ))

    if user.role != Role.SUPER_ADMIN:
        # every usable dataset belongs to one class (subject + grade) - one
        # missing either is visible to no school, never "to everyone"
        query = query.where(Dataset.grade.is_not(None), Dataset.subject_id.is_not(None))
    if user.role == Role.TEACHER:
        pairs = (
            await session.execute(
                select(SchoolClass.grade, TeacherClassAssignment.subject_id)
                .join(SchoolClass, SchoolClass.id == TeacherClassAssignment.class_id)
                .where(TeacherClassAssignment.teacher_id == user.id, SchoolClass.school_id == user.school_id)
                .distinct()
            )
        ).all()
        if not pairs:
            return []
        query = query.where(or_(*(and_(Dataset.grade == g, Dataset.subject_id == s) for g, s in pairs)))
    elif user.role == Role.STUDENT:
        student_grade = (
            await session.execute(
                select(SchoolClass.grade)
                .join(StudentProfile, StudentProfile.class_id == SchoolClass.id)
                .where(StudentProfile.user_id == user.id, SchoolClass.school_id == user.school_id)
            )
        ).scalar_one_or_none()
        if student_grade is None:
            return []
        query = query.where(Dataset.grade == student_grade)

    if subject_id is not None:
        query = query.where(Dataset.subject_id == subject_id)
    if grade is not None:
        query = query.where(Dataset.grade == grade)

    return list((await session.execute(query.order_by(Dataset.name))).scalars().all())


async def ensure_datasets_visible(
    session: AsyncSession,
    user: User,
    dataset_ids: list[str],
    *,
    subject_id: str | UUID | None = None,
    grade: int | None = None,
) -> None:
    """Raise ForbiddenError if any of `dataset_ids` isn't in visible_datasets()."""
    allowed = {str(d.id) for d in await visible_datasets(session, user, subject_id=subject_id, grade=grade)}
    denied = [d for d in dataset_ids if str(d) not in allowed]
    if denied:
        raise ForbiddenError(f"Dataset(s) not available for this class: {', '.join(map(str, denied))}")


async def enabled_subject_ids(session: AsyncSession, school_id) -> set:
    """Subjects a Super Admin has switched on for the school
    (school_curriculum). None enabled = none - schools don't control
    subjects and there's no "not configured -> everything" fallback."""
    rows = (await session.execute(
        select(SchoolCurriculum.subject_id).where(
            SchoolCurriculum.school_id == school_id, SchoolCurriculum.enabled.is_(True))
    )).all()
    return {subject_id for (subject_id,) in rows}


async def school_dataset_listing(
    session: AsyncSession, school_id: str | UUID, *, include_unassigned: bool
) -> list[tuple[Dataset, bool, bool, bool]]:
    """(dataset, assigned, owned, subject_enabled) for one school: its own datasets plus the
    catalog datasets assigned to it - and, for the Super Admin's assignment
    screen (`include_unassigned`), every other catalog dataset too. Never
    another school's own datasets."""
    assigned = {
        row[0] for row in (await session.execute(
            select(SchoolDataset.dataset_id).where(
                SchoolDataset.school_id == school_id, SchoolDataset.enabled.is_(True))
        )).all()
    }
    rows = (await session.execute(
        select(Dataset).where(or_(Dataset.school_id == school_id, Dataset.school_id.is_(None)))
        .order_by(Dataset.school_id.is_(None), Dataset.name)
    )).scalars().all()
    subjects_on = await enabled_subject_ids(session, school_id)
    out = []
    for d in rows:
        owned = d.school_id is not None
        is_assigned = owned or d.id in assigned
        subject_on = d.subject_id is None or d.subject_id in subjects_on
        if is_assigned or include_unassigned:
            out.append((d, is_assigned, owned, subject_on))
    return out


async def subject_names(session: AsyncSession, datasets) -> dict:
    """subject_id -> name for the datasets' subjects (for listings)."""
    ids = {d.subject_id for d in datasets if d.subject_id}
    if not ids:
        return {}
    return dict((await session.execute(select(Subject.id, Subject.name).where(Subject.id.in_(ids)))).all())


async def assign_catalog_datasets(session: AsyncSession, school_id: str | UUID, dataset_ids: list[str]) -> None:
    """Super Admin only: make exactly `dataset_ids` the catalog datasets
    assigned to the school. Only catalog datasets (school_id NULL) can be
    assigned - a school's own dataset can't be handed to anyone else."""
    wanted = set(dataset_ids)
    if wanted:
        catalog_ids = {
            str(row[0]) for row in (await session.execute(
                select(Dataset.id).where(Dataset.id.in_(wanted), Dataset.school_id.is_(None))
            )).all()
        }
        if not_catalog := wanted - catalog_ids:
            raise ValidationError(
                f"Only platform catalog datasets can be assigned: {', '.join(sorted(not_catalog))}")
    existing = {
        str(row.dataset_id): row for row in (await session.execute(
            select(SchoolDataset).where(SchoolDataset.school_id == school_id))).scalars().all()
    }
    for dataset_id, row in existing.items():
        row.enabled = dataset_id in wanted
    for dataset_id in wanted - existing.keys():
        session.add(SchoolDataset(school_id=school_id, dataset_id=dataset_id, enabled=True))
