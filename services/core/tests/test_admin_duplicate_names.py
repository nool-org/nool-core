"""Creating/renaming a Plan or Subject with a name that already exists
must 409 (ConflictError), not 500 - both `name` columns are DB-unique, and
letting the insert/update hit that constraint directly aborts the
session's transaction (same reasoning as school_admin.py's `_row_exists`
pre-check pattern). Real rows against the dev Postgres via db_session.
"""

import uuid

import pytest

from shared.errors import ConflictError

from src.api.routes import admin, admin_catalog
from src.api.schemas.admin import CreatePlanIn, UpdatePlanIn
from src.api.schemas.admin_catalog import CreateSubjectIn, UpdateSubjectIn
from src.domain.models import Plan, Role, Subject, User, UserStatus


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


async def test_create_plan_with_duplicate_name_conflicts_not_500(db_session):
    super_admin = await _seed_super_admin(db_session)
    name = f"Plan-{uuid.uuid4()}"
    db_session.add(Plan(name=name, price_label="x", teacher_limit=1, student_limit=1))
    await db_session.flush()

    with pytest.raises(ConflictError):
        await admin.create_plan(
            CreatePlanIn(name=name, price_label="y", teacher_limit=2, student_limit=2),
            actor=super_admin, session=db_session,
        )


async def test_rename_plan_to_a_duplicate_name_conflicts_not_500(db_session):
    super_admin = await _seed_super_admin(db_session)
    taken_name = f"Plan-{uuid.uuid4()}"
    db_session.add(Plan(name=taken_name, price_label="x", teacher_limit=1, student_limit=1))
    other = Plan(name=f"Plan-{uuid.uuid4()}", price_label="y", teacher_limit=1, student_limit=1)
    db_session.add(other)
    await db_session.flush()

    with pytest.raises(ConflictError):
        await admin.update_plan(
            str(other.id), UpdatePlanIn(name=taken_name), actor=super_admin, session=db_session,
        )


async def test_rename_plan_to_its_own_current_name_does_not_conflict(db_session):
    super_admin = await _seed_super_admin(db_session)
    name = f"Plan-{uuid.uuid4()}"
    plan = Plan(name=name, price_label="x", teacher_limit=1, student_limit=1)
    db_session.add(plan)
    await db_session.flush()

    out = await admin.update_plan(
        str(plan.id), UpdatePlanIn(name=name, price_label="new"), actor=super_admin, session=db_session,
    )

    assert out.price_label == "new"


async def test_create_subject_with_duplicate_name_for_the_same_grade_conflicts_not_500(db_session):
    super_admin = await _seed_super_admin(db_session)
    name = f"Subject-{uuid.uuid4()}"
    db_session.add(Subject(name=name, grade=9))
    await db_session.flush()

    with pytest.raises(ConflictError):
        await admin_catalog.create_subject(
            CreateSubjectIn(name=name.lower(), grade=9), actor=super_admin, session=db_session
        )


async def test_the_same_subject_name_for_another_grade_is_a_separate_subject(db_session):
    # "Science" grade 9 and "Science" grade 10 each have their own syllabus
    super_admin = await _seed_super_admin(db_session)
    name = f"Science-{uuid.uuid4()}"
    db_session.add(Subject(name=name, grade=9))
    await db_session.flush()

    created = await admin_catalog.create_subject(
        CreateSubjectIn(name=name, grade=10), actor=super_admin, session=db_session
    )
    assert (created.name, created.grade, created.grades) == (name, 10, [10])


async def test_a_subject_needs_exactly_one_grade():
    with pytest.raises(ValueError):
        CreateSubjectIn(name="Science", grades=[9, 10])
    with pytest.raises(ValueError):
        CreateSubjectIn(name="Science")
    assert CreateSubjectIn(name="Science", grades=[9]).grade == 9


async def test_rename_subject_to_a_duplicate_name_for_the_same_grade_conflicts_not_500(db_session):
    super_admin = await _seed_super_admin(db_session)
    taken_name = f"Subject-{uuid.uuid4()}"
    db_session.add(Subject(name=taken_name, grade=9))
    other = Subject(name=f"Subject-{uuid.uuid4()}", grade=9)
    db_session.add(other)
    await db_session.flush()

    with pytest.raises(ConflictError):
        await admin_catalog.update_subject(
            str(other.id), UpdateSubjectIn(name=taken_name, grade=9), actor=super_admin, session=db_session,
        )
