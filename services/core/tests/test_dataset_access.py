"""A class-scoped Dataset (grade + subject set) is only visible to users of
that class - see repositories/dataset_access.py. Asserts on the ids this
test creates only, since the dev database may already hold other catalog
datasets.
"""

import uuid

import pytest
from shared.errors import ForbiddenError

from src.domain.models import (
    Dataset, Role, School, SchoolClass, SchoolCurriculum, SchoolDataset, StudentProfile, Subject, TeacherClassAssignment, User,
    UserStatus,
)
from src.repositories.dataset_access import ensure_datasets_visible, visible_datasets


async def _seed(db_session) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
    science, maths = Subject(name=f"Science-{uuid.uuid4()}"), Subject(name=f"Maths-{uuid.uuid4()}")
    db_session.add_all([school, science, maths])
    await db_session.flush()

    ten_a = SchoolClass(school_id=school.id, grade=10, section="A")
    db_session.add(ten_a)
    await db_session.flush()

    def user(role):
        return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=role.value,
                    role=role, school_id=school.id, status=UserStatus.ACTIVE)

    teacher, student, admin = user(Role.TEACHER), user(Role.STUDENT), user(Role.SCHOOL_ADMIN)
    db_session.add_all([teacher, student, admin])
    await db_session.flush()
    db_session.add_all([
        TeacherClassAssignment(teacher_id=teacher.id, class_id=ten_a.id, subject_id=science.id),
        StudentProfile(user_id=student.id, class_id=ten_a.id, roll_number=1),
    ])

    def dataset(name, grade, subject):
        return Dataset(name=name, question_count=0, description="", grade=grade,
                       subject_id=subject.id if subject else None, board="CBSE" if grade else None)

    ds = {
        "sci10": dataset("Sci 10", 10, science),
        "maths10": dataset("Maths 10", 10, maths),
        "sci9": dataset("Sci 9", 9, science),
        "unscoped": dataset("Unscoped", None, None),
    }
    db_session.add_all(ds.values())
    await db_session.flush()
    # assignment-only access: a Super Admin assigns catalog datasets to the school
    db_session.add_all([SchoolDataset(school_id=school.id, dataset_id=d.id, enabled=True) for d in ds.values()])
    # ...and gives the school its subjects
    db_session.add_all([SchoolCurriculum(school_id=school.id, subject_id=sub.id, enabled=True) for sub in (science, maths)])
    await db_session.flush()
    return {"teacher": teacher, "student": student, "admin": admin, "science": science, "ds": ds, "school": school}


async def _visible(db_session, user, **kw) -> set[str]:
    return {d.name for d in await visible_datasets(db_session, user, **kw)} & {"Sci 10", "Maths 10", "Sci 9", "Unscoped"}


@pytest.mark.asyncio
async def test_teacher_sees_only_the_grade_and_subject_they_teach(db_session):
    s = await _seed(db_session)
    # "Unscoped" (no subject/grade) is assigned but belongs to no class, so nobody may use it
    assert await _visible(db_session, s["teacher"]) == {"Sci 10"}


@pytest.mark.asyncio
async def test_student_sees_every_subject_of_their_own_grade_only(db_session):
    s = await _seed(db_session)
    assert await _visible(db_session, s["student"]) == {"Sci 10", "Maths 10"}


@pytest.mark.asyncio
async def test_school_admin_is_not_class_scoped_but_can_narrow(db_session):
    s = await _seed(db_session)
    assert await _visible(db_session, s["admin"]) == {"Sci 10", "Maths 10", "Sci 9"}
    assert await _visible(db_session, s["admin"], grade=9, subject_id=s["science"].id) == {"Sci 9"}


@pytest.mark.asyncio
async def test_ensure_rejects_a_dataset_from_another_grade(db_session):
    s = await _seed(db_session)
    await ensure_datasets_visible(db_session, s["teacher"], [str(s["ds"]["sci10"].id)])
    with pytest.raises(ForbiddenError):
        await ensure_datasets_visible(db_session, s["teacher"], [str(s["ds"]["sci9"].id)])


@pytest.mark.asyncio
async def test_a_schools_own_dataset_is_visible_only_to_that_school(db_session):
    s = await _seed(db_session)
    own = Dataset(name="Our unit tests", question_count=0, description="", grade=10,
                  subject_id=s["science"].id, board="CBSE", school_id=s["school"].id)
    other_school = School(name=f"Other-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add_all([own, other_school])
    await db_session.flush()
    theirs = Dataset(name="Their unit tests", question_count=0, description="", grade=10,
                     subject_id=s["science"].id, board="CBSE", school_id=other_school.id)
    unassigned = Dataset(name="Unassigned catalog", question_count=0, description="")
    db_session.add_all([theirs, unassigned])
    await db_session.flush()

    names = {d.name for d in await visible_datasets(db_session, s["teacher"])}
    assert "Our unit tests" in names
    assert "Their unit tests" not in names and "Unassigned catalog" not in names
    with pytest.raises(ForbiddenError):
        await ensure_datasets_visible(db_session, s["teacher"], [str(theirs.id)])
