"""Super Admin's cross-tenant Curriculum/Dataset toggles (src/api/routes/
admin.py) - mirrors school_admin.py's own /curriculum and /datasets
sections, but scoped by an explicit school_id instead of user.school_id.
Real rows against the dev Postgres via db_session, same pattern as
test_admin_classes.py.
"""

import uuid

import pytest

from shared.errors import ConflictError, NotFoundError

from src.api.routes import admin
from src.api.schemas.school_admin import (
    CreateSchoolSubjectIn,
    UpdateSchoolCurriculumIn,
    UpdateSchoolDatasetsIn,
)
from src.domain.models import Dataset, Role, School, Subject, SubjectGrade, User, UserStatus


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_subject(db_session, *, grades: list[int] | None = None) -> Subject:
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    for grade in grades or [9]:
        db_session.add(SubjectGrade(subject_id=subject.id, grade=grade))
    await db_session.flush()
    return subject


async def _seed_dataset(db_session) -> Dataset:
    dataset = Dataset(name=f"Dataset-{uuid.uuid4()}", question_count=10, description="A dataset")
    db_session.add(dataset)
    await db_session.flush()
    return dataset


async def test_get_curriculum_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.get_curriculum_as_admin(str(uuid.uuid4()), _=super_admin, session=db_session)


async def test_update_curriculum_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.update_curriculum_as_admin(
            str(uuid.uuid4()), UpdateSchoolCurriculumIn(subject_ids=[]), actor=super_admin, session=db_session
        )


async def test_create_subject_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.create_subject_as_admin(
            str(uuid.uuid4()), CreateSchoolSubjectIn(name=f"Subject-{uuid.uuid4()}", grades=[9]),
            actor=super_admin, session=db_session,
        )


async def test_get_datasets_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.get_datasets_as_admin(str(uuid.uuid4()), _=super_admin, session=db_session)


async def test_update_datasets_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.update_datasets_as_admin(
            str(uuid.uuid4()), UpdateSchoolDatasetsIn(dataset_ids=[]), actor=super_admin, session=db_session
        )


async def test_curriculum_enable_disable_round_trip(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    subject = await _seed_subject(db_session)

    enabled = await admin.update_curriculum_as_admin(
        str(school.id), UpdateSchoolCurriculumIn(subject_ids=[str(subject.id)]),
        actor=super_admin, session=db_session,
    )
    toggle = next(s for s in enabled.subjects if s.id == str(subject.id))
    assert toggle.enabled is True

    disabled = await admin.update_curriculum_as_admin(
        str(school.id), UpdateSchoolCurriculumIn(subject_ids=[]), actor=super_admin, session=db_session
    )
    toggle2 = next(s for s in disabled.subjects if s.id == str(subject.id))
    assert toggle2.enabled is False

    fetched = await admin.get_curriculum_as_admin(str(school.id), _=super_admin, session=db_session)
    toggle3 = next(s for s in fetched.subjects if s.id == str(subject.id))
    assert toggle3.enabled is False


async def test_update_curriculum_enables_a_new_subject_alongside_an_already_enabled_one(db_session):
    """Regression test: subject_ids off the wire is list[str] while
    SchoolCurriculum.subject_id is a UUID column - diffing them without
    normalizing to the same type treats every already-enabled subject as
    "new" and tries to re-insert it, hitting the (school_id, subject_id)
    unique constraint the moment a PUT enables one more subject alongside
    an existing one (found live via the Super Admin curriculum page).
    """
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    subject_a = await _seed_subject(db_session)
    subject_b = await _seed_subject(db_session)

    await admin.update_curriculum_as_admin(
        str(school.id), UpdateSchoolCurriculumIn(subject_ids=[str(subject_a.id)]),
        actor=super_admin, session=db_session,
    )

    result = await admin.update_curriculum_as_admin(
        str(school.id), UpdateSchoolCurriculumIn(subject_ids=[str(subject_a.id), str(subject_b.id)]),
        actor=super_admin, session=db_session,
    )

    enabled_ids = {s.id for s in result.subjects if s.enabled}
    assert enabled_ids == {str(subject_a.id), str(subject_b.id)}


async def test_curriculum_cross_school_isolation(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    subject = await _seed_subject(db_session)

    await admin.update_curriculum_as_admin(
        str(school_a.id), UpdateSchoolCurriculumIn(subject_ids=[str(subject.id)]),
        actor=super_admin, session=db_session,
    )

    view_a = await admin.get_curriculum_as_admin(str(school_a.id), _=super_admin, session=db_session)
    view_b = await admin.get_curriculum_as_admin(str(school_b.id), _=super_admin, session=db_session)

    toggle_a = next(s for s in view_a.subjects if s.id == str(subject.id))
    toggle_b = next(s for s in view_b.subjects if s.id == str(subject.id))
    assert toggle_a.enabled is True
    assert toggle_b.enabled is False


async def test_create_subject_as_admin_reuses_existing_subject_by_name_across_schools(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    name = f"Sanskrit-{uuid.uuid4()}"

    result_a = await admin.create_subject_as_admin(
        str(school_a.id), CreateSchoolSubjectIn(name=name, grades=[9]), actor=super_admin, session=db_session
    )
    result_b = await admin.create_subject_as_admin(
        str(school_b.id), CreateSchoolSubjectIn(name=name, grades=[9]), actor=super_admin, session=db_session
    )

    # Same underlying Subject row reused, not duplicated.
    assert result_a.id == result_b.id
    assert result_a.enabled is True
    assert result_b.enabled is True

    # Enabled independently for each school - disabling for A doesn't
    # affect B.
    await admin.update_curriculum_as_admin(
        str(school_a.id), UpdateSchoolCurriculumIn(subject_ids=[]), actor=super_admin, session=db_session
    )
    view_a = await admin.get_curriculum_as_admin(str(school_a.id), _=super_admin, session=db_session)
    view_b = await admin.get_curriculum_as_admin(str(school_b.id), _=super_admin, session=db_session)
    toggle_a = next(s for s in view_a.subjects if s.id == result_a.id)
    toggle_b = next(s for s in view_b.subjects if s.id == result_b.id)
    assert toggle_a.enabled is False
    assert toggle_b.enabled is True


async def test_create_subject_as_admin_rejects_empty_name(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    with pytest.raises(ConflictError):
        await admin.create_subject_as_admin(
            str(school.id), CreateSchoolSubjectIn(name="   ", grades=[9]), actor=super_admin, session=db_session
        )


async def test_datasets_enable_disable_round_trip(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    dataset = await _seed_dataset(db_session)

    enabled = await admin.update_datasets_as_admin(
        str(school.id), UpdateSchoolDatasetsIn(dataset_ids=[str(dataset.id)]),
        actor=super_admin, session=db_session,
    )
    item = next(d for d in enabled.items if d.id == str(dataset.id))
    assert item.enabled is True

    disabled = await admin.update_datasets_as_admin(
        str(school.id), UpdateSchoolDatasetsIn(dataset_ids=[]), actor=super_admin, session=db_session
    )
    item2 = next(d for d in disabled.items if d.id == str(dataset.id))
    assert item2.enabled is False

    fetched = await admin.get_datasets_as_admin(str(school.id), _=super_admin, session=db_session)
    item3 = next(d for d in fetched.items if d.id == str(dataset.id))
    assert item3.enabled is False


async def test_datasets_cross_school_isolation(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    dataset = await _seed_dataset(db_session)

    await admin.update_datasets_as_admin(
        str(school_a.id), UpdateSchoolDatasetsIn(dataset_ids=[str(dataset.id)]),
        actor=super_admin, session=db_session,
    )

    view_a = await admin.get_datasets_as_admin(str(school_a.id), _=super_admin, session=db_session)
    view_b = await admin.get_datasets_as_admin(str(school_b.id), _=super_admin, session=db_session)

    item_a = next(d for d in view_a.items if d.id == str(dataset.id))
    item_b = next(d for d in view_b.items if d.id == str(dataset.id))
    assert item_a.enabled is True
    assert item_b.enabled is False
