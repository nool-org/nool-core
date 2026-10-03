"""GET /datasets (teacher-facing): a school sees only the catalog datasets a
Super Admin assigned to it - there's no "not configured -> everything"
fallback, and the old `restricted` flag no longer changes anything (every
catalog dataset needs an assignment). Datasets must belong to one class
(subject + grade) to be usable at all.
"""

import uuid

import pytest

from src.api.routes import dataset as dataset_routes
from src.domain.models import (
    Dataset, Role, School, SchoolClass, SchoolCurriculum, SchoolDataset, Subject, TeacherClassAssignment, User,
    UserStatus,
)


async def _seed_teacher(db_session) -> tuple[User, School, Subject]:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=10)
    db_session.add_all([school, subject])
    await db_session.flush()
    teacher = User(
        firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    section = SchoolClass(school_id=school.id, grade=10, section="A")
    db_session.add_all([teacher, section])
    await db_session.flush()
    db_session.add_all([
        TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
        SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True),
    ])
    await db_session.flush()
    return teacher, school, subject


def _dataset(name, subject, **kw):
    return Dataset(name=name, question_count=10, description="", subject_id=subject.id, grade=10, **kw)


async def _names(db_session, teacher):
    return {item.name for item in (await dataset_routes.list_datasets(user=teacher, session=db_session)).items}


@pytest.mark.asyncio
async def test_a_school_with_no_assignments_sees_no_catalog_dataset_at_all(db_session):
    teacher, _school, subject = await _seed_teacher(db_session)
    db_session.add_all([_dataset("MVM-only bank", subject, restricted=True), _dataset("Shared catalog dataset", subject)])
    await db_session.flush()

    names = await _names(db_session, teacher)
    assert "Shared catalog dataset" not in names and "MVM-only bank" not in names


@pytest.mark.asyncio
async def test_an_assigned_dataset_shows_up_restricted_or_not(db_session):
    teacher, school, subject = await _seed_teacher(db_session)
    restricted = _dataset("MVM-only bank", subject, restricted=True)
    db_session.add(restricted)
    await db_session.flush()
    db_session.add(SchoolDataset(school_id=school.id, dataset_id=restricted.id, enabled=True))
    await db_session.flush()

    assert "MVM-only bank" in await _names(db_session, teacher)


@pytest.mark.asyncio
async def test_only_the_assigned_one_shows_when_the_school_has_other_assignments(db_session):
    teacher, school, subject = await _seed_teacher(db_session)
    restricted = _dataset("MVM-only bank", subject, restricted=True)
    unrelated = _dataset("Unrelated dataset", subject)
    no_class = Dataset(name="No subject/grade", question_count=1, description="")   # usable by nobody
    db_session.add_all([restricted, unrelated, no_class])
    await db_session.flush()
    db_session.add_all([SchoolDataset(school_id=school.id, dataset_id=d.id, enabled=True) for d in (unrelated, no_class)])
    await db_session.flush()

    names = await _names(db_session, teacher)
    assert "Unrelated dataset" in names
    assert "MVM-only bank" not in names and "No subject/grade" not in names
