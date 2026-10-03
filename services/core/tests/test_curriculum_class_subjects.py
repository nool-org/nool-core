"""GET /classes/{class_id}/subjects must never surface a subject the
school has since disabled (or that a TeacherClassAssignment row was wrong
about from the start) - a real production incident: a teacher's
assignment row pointed at a leftover "Smoke Test Subject" instead of the
school's real, enabled subject, and this endpoint blindly trusted it since
it never cross-checked school_curriculum at all (unlike GET /subjects).
"""

import uuid

from src.api.routes import curriculum
from src.domain.models import (
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    Subject,
    TeacherClassAssignment,
    User,
    UserStatus,
)


async def _seed_school_and_teacher(db_session) -> tuple[User, School]:
    school = School(name="Curriculum Test School", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()

    teacher = User(
        firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    return teacher, school


async def _seed_class_assignment(db_session, *, teacher: User, school: School, subject: Subject) -> str:
    school_class = SchoolClass(school_id=school.id, grade=10, section="A")
    db_session.add(school_class)
    await db_session.flush()
    db_session.add(
        TeacherClassAssignment(teacher_id=teacher.id, class_id=school_class.id, subject_id=subject.id)
    )
    await db_session.flush()
    return str(school_class.id)


async def test_a_stale_assignment_to_a_since_disabled_subject_is_not_surfaced(db_session):
    """Exactly the real-world bug: the assignment points at a subject the
    school has explicitly disabled (a real subject is enabled instead) -
    the endpoint must return empty, not the disabled one.
    """
    teacher, school = await _seed_school_and_teacher(db_session)
    real_subject = Subject(name=f"Science-{uuid.uuid4()}")
    disabled_subject = Subject(name=f"Smoke Test Subject-{uuid.uuid4()}")
    db_session.add_all([real_subject, disabled_subject])
    await db_session.flush()

    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=real_subject.id, enabled=True))
    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=disabled_subject.id, enabled=False))
    await db_session.flush()

    class_id = await _seed_class_assignment(
        db_session, teacher=teacher, school=school, subject=disabled_subject
    )

    result = await curriculum.list_subjects_for_class(class_id, user=teacher, session=db_session)

    assert result.items == []


async def test_an_assignment_to_a_currently_enabled_subject_is_still_returned(db_session):
    teacher, school = await _seed_school_and_teacher(db_session)
    subject = Subject(name=f"Mathematics-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True))
    await db_session.flush()

    class_id = await _seed_class_assignment(db_session, teacher=teacher, school=school, subject=subject)

    result = await curriculum.list_subjects_for_class(class_id, user=teacher, session=db_session)

    assert [item.name for item in result.items] == [subject.name]


async def test_a_school_the_super_admin_gave_no_subjects_has_none_even_with_an_assignment(db_session):
    """Only a Super Admin controls a school's subjects (school_curriculum) -
    there's no "not configured -> every subject" fallback any more."""
    teacher, school = await _seed_school_and_teacher(db_session)
    subject = Subject(name=f"Mathematics-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()

    class_id = await _seed_class_assignment(db_session, teacher=teacher, school=school, subject=subject)

    result = await curriculum.list_subjects_for_class(class_id, user=teacher, session=db_session)

    assert result.items == []
