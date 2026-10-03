"""Super Admin's cross-tenant Class/Roster management (src/api/routes/admin.py)
- mirrors school_admin.py's own Classes section, but scoped by an explicit
school_id (list/create) or not scoped at all (a class_id is globally
unique, so Super Admin can act on any class at any school). Real rows
against the dev Postgres via db_session, same pattern as
test_admin_teachers.py/test_admin_students.py.
"""

import uuid

import pytest
from sqlalchemy import select

from shared.errors import ConflictError, NotFoundError

from src.api.routes import admin
from src.api.schemas.school_admin import (
    ClassAssignmentOut,
    CreateClassIn,
    UpdateClassAssignmentsIn,
    UpdateClassIn,
    UpdateClassStatusIn,
)
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    Homework,
    HomeworkDifficulty,
    Role,
    School,
    SchoolClass,
    StudentProfile,
    Subject,
    TeacherClassAssignment,
    TestStatus,
    User,
    UserStatus,
    VoiceTest,
)


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


async def _seed_class(db_session, school_id, *, grade=5, section=None) -> SchoolClass:
    section = section or f"S{uuid.uuid4().hex[:8]}"
    school_class = SchoolClass(school_id=school_id, grade=grade, section=section)
    db_session.add(school_class)
    await db_session.flush()
    return school_class


async def _seed_teacher(db_session, school_id) -> User:
    teacher = User(
        firebase_uid=f"teacher-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    return teacher


async def _seed_student(db_session, school_id, class_id, *, roll_number=1) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    db_session.add(StudentProfile(user_id=student.id, class_id=class_id, roll_number=roll_number))
    await db_session.flush()
    return student


async def _seed_subject_chapter(db_session) -> tuple[Subject, Chapter]:
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    return subject, chapter


async def _seed_voice_test(db_session, school_class, subject, chapter) -> VoiceTest:
    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48,
        target_mode=AssignmentTargetMode.WHOLE_CLASS, status=TestStatus.DRAFT,
    )
    db_session.add(test)
    await db_session.flush()
    return test


async def test_list_classes_as_admin_scoped_to_one_school_only(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    class_a = await _seed_class(db_session, school_a.id, grade=6)
    await _seed_class(db_session, school_b.id, grade=7)

    result = await admin.list_classes_as_admin(str(school_a.id), _=super_admin, session=db_session)

    assert result.total == 1
    assert result.items[0].id == str(class_a.id)
    assert result.items[0].grade == 6


async def test_list_classes_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.list_classes_as_admin(str(uuid.uuid4()), _=super_admin, session=db_session)


async def test_create_class_as_admin_creates_in_target_school(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    section = f"S{uuid.uuid4().hex[:8]}"

    result = await admin.create_class_as_admin(
        str(school.id), CreateClassIn(grade=8, section=section), actor=super_admin, session=db_session
    )

    assert result.grade == 8
    assert result.section == section
    assert result.student_count == 0
    assert result.assignments == []


async def test_create_class_as_admin_unknown_school_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.create_class_as_admin(
            str(uuid.uuid4()), CreateClassIn(grade=8, section=f"S{uuid.uuid4().hex[:8]}"),
            actor=super_admin, session=db_session,
        )


async def test_update_class_as_admin_no_school_filter(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id, grade=5)
    new_section = f"S{uuid.uuid4().hex[:8]}"

    out = await admin.update_class_as_admin(
        str(school_class.id), UpdateClassIn(grade=9, section=new_section), actor=super_admin, session=db_session
    )

    assert out.grade == 9
    assert out.section == new_section


async def test_update_class_as_admin_unknown_class_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.update_class_as_admin(
            str(uuid.uuid4()), UpdateClassIn(grade=5, section=f"S{uuid.uuid4().hex[:8]}"),
            actor=super_admin, session=db_session,
        )


async def test_update_class_status_as_admin_round_trips(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id)

    out = await admin.update_class_status_as_admin(
        str(school_class.id), UpdateClassStatusIn(status=UserStatus.DEACTIVATED),
        actor=super_admin, session=db_session,
    )
    assert out.status == UserStatus.DEACTIVATED

    out2 = await admin.update_class_status_as_admin(
        str(school_class.id), UpdateClassStatusIn(status=UserStatus.ACTIVE),
        actor=super_admin, session=db_session,
    )
    assert out2.status == UserStatus.ACTIVE


async def test_delete_class_as_admin_blocked_when_students_present(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id)
    await _seed_student(db_session, school.id, school_class.id)

    with pytest.raises(ConflictError):
        await admin.delete_class_as_admin(str(school_class.id), actor=super_admin, session=db_session)


async def test_delete_class_as_admin_blocked_when_test_activity_present(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id)
    subject, chapter = await _seed_subject_chapter(db_session)
    await _seed_voice_test(db_session, school_class, subject, chapter)

    with pytest.raises(ConflictError):
        await admin.delete_class_as_admin(str(school_class.id), actor=super_admin, session=db_session)


async def test_delete_class_as_admin_blocked_when_homework_activity_present(db_session):
    """Homework.class_id is what should trip the block here, not
    VoiceTest.class_id - the underlying VoiceTest row (Homework.test_id is a
    required FK) is attached to a *different* class so this isolates which
    check actually fires.
    """
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id)
    other_class = await _seed_class(db_session, school.id)
    subject, chapter = await _seed_subject_chapter(db_session)
    test = await _seed_voice_test(db_session, other_class, subject, chapter)
    homework = Homework(
        test_id=test.id, class_id=school_class.id, gap_topic="Gap topic",
        gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
        target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(homework)
    await db_session.flush()

    with pytest.raises(ConflictError):
        await admin.delete_class_as_admin(str(school_class.id), actor=super_admin, session=db_session)


async def test_delete_class_as_admin_deletes_when_clean_and_is_idempotent(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id)

    result = await admin.delete_class_as_admin(str(school_class.id), actor=super_admin, session=db_session)
    assert result == {"deleted": True}

    # Idempotent: deleting again (already gone) still reports deleted=True.
    result2 = await admin.delete_class_as_admin(str(school_class.id), actor=super_admin, session=db_session)
    assert result2 == {"deleted": True}


async def test_delete_class_as_admin_unknown_class_is_idempotent(db_session):
    super_admin = await _seed_super_admin(db_session)
    result = await admin.delete_class_as_admin(str(uuid.uuid4()), actor=super_admin, session=db_session)
    assert result == {"deleted": True}


async def test_update_class_assignments_as_admin_full_replace(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id)
    teacher_1 = await _seed_teacher(db_session, school.id)
    teacher_2 = await _seed_teacher(db_session, school.id)
    subject_1, _chapter_1 = await _seed_subject_chapter(db_session)
    subject_2, _chapter_2 = await _seed_subject_chapter(db_session)
    from src.domain.models import SchoolCurriculum
    db_session.add_all([SchoolCurriculum(school_id=school.id, subject_id=s.id, enabled=True) for s in (subject_1, subject_2)])
    await db_session.flush()

    first = await admin.update_class_assignments_as_admin(
        str(school_class.id),
        UpdateClassAssignmentsIn(
            assignments=[ClassAssignmentOut(teacher_id=str(teacher_1.id), subject_id=str(subject_1.id))]
        ),
        actor=super_admin, session=db_session,
    )
    assert len(first.assignments) == 1
    assert first.assignments[0].teacher_id == str(teacher_1.id)

    second = await admin.update_class_assignments_as_admin(
        str(school_class.id),
        UpdateClassAssignmentsIn(
            assignments=[ClassAssignmentOut(teacher_id=str(teacher_2.id), subject_id=str(subject_2.id))]
        ),
        actor=super_admin, session=db_session,
    )
    assert len(second.assignments) == 1
    assert second.assignments[0].teacher_id == str(teacher_2.id)
    assert second.assignments[0].subject_id == str(subject_2.id)

    # Confirm the old assignment row is really gone from the DB, not just
    # excluded from the DTO.
    remaining = await db_session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == school_class.id)
    )
    rows = remaining.scalars().all()
    assert len(rows) == 1
    assert rows[0].teacher_id == teacher_2.id


async def test_update_class_assignments_as_admin_unknown_class_404s(db_session):
    super_admin = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin.update_class_assignments_as_admin(
            str(uuid.uuid4()), UpdateClassAssignmentsIn(assignments=[]), actor=super_admin, session=db_session
        )
