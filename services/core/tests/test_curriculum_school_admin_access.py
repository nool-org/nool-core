"""GET /subjects, /subjects/{id}/chapters, /chapters/{id}/topics are now
also callable by SCHOOL_ADMIN (previously TEACHER-only) - the read
surface School Admin's new "Add custom question" form builds its
Subject -> Chapter -> Topic cascade from. Real rows against the dev
Postgres via db_session.
"""

import uuid

from src.api.routes import curriculum
from src.domain.models import Chapter, Role, School, Subject, Topic, User, UserStatus, SchoolCurriculum


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_school_admin(db_session, school_id) -> User:
    admin = User(
        firebase_uid=f"admin-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Admin", role=Role.SCHOOL_ADMIN, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()
    return admin


async def test_school_admin_can_list_subjects_chapters_and_topics(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True))  # Super Admin's grant
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="Topic 1")
    db_session.add(topic)
    await db_session.flush()

    subjects = await curriculum.list_subjects(user=admin, session=db_session)
    assert any(s.id == str(subject.id) for s in subjects.items)

    chapters = await curriculum.list_chapters(str(subject.id), user=admin, session=db_session)
    assert any(c.id == str(chapter.id) for c in chapters.items)

    topics = await curriculum.list_topics(str(chapter.id), user=admin, session=db_session)
    assert any(t.id == str(topic.id) for t in topics.items)


async def test_student_can_list_chapters_only_of_a_subject_taught_in_their_class(db_session):
    """S-001: Learn's syllabus called /subjects/{id}/chapters as a student and got 403."""
    import pytest
    from shared.errors import NotFoundError
    from src.domain.models import SchoolClass, StudentProfile, TeacherClassAssignment

    school = await _seed_school(db_session)
    taught, other = Subject(name=f"Subject-{uuid.uuid4()}"), Subject(name=f"Subject-{uuid.uuid4()}")
    section = SchoolClass(school_id=school.id, grade=10, section=str(uuid.uuid4())[:8])
    db_session.add_all([taught, other, section])
    await db_session.flush()
    teacher, student = (User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="U",
                             role=role, school_id=school.id, status=UserStatus.ACTIVE) for role in (Role.TEACHER, Role.STUDENT))
    db_session.add_all([teacher, student])
    await db_session.flush()
    db_session.add_all([
        SchoolCurriculum(school_id=school.id, subject_id=taught.id, enabled=True),
        SchoolCurriculum(school_id=school.id, subject_id=other.id, enabled=True),
        StudentProfile(user_id=student.id, class_id=section.id, roll_number=1),
        TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=taught.id),
        Chapter(subject_id=taught.id, name="Real Numbers"),
    ])
    await db_session.flush()

    route = next(r for r in curriculum.router.routes if r.path == "/api/v1/subjects/{subject_id}/chapters")
    role_check = next(d for d in route.dependant.dependencies if d.name == "user")
    assert await role_check.call(user=student) is student  # was a 403
    chapters = await curriculum.list_chapters(str(taught.id), user=student, session=db_session)
    assert [c.name for c in chapters.items] == ["Real Numbers"]
    with pytest.raises(NotFoundError):  # enabled for the school, but not taught in their class
        await curriculum.list_chapters(str(other.id), user=student, session=db_session)
