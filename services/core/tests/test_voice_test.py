"""Test (Voice Test) creation - specifically the WHOLE_CLASS delivery bug:
VoiceTestTargetStudent used to only ever be populated for
SPECIFIC_STUDENTS, so a WHOLE_CLASS test reached zero students through
every read path a student's own app uses (assigned_test.py,
student_dashboard.py). Real rows against the dev Postgres via db_session,
same pattern as test_reporting.py.
"""

import uuid

from fastapi import BackgroundTasks
from sqlalchemy import select

from src.api.routes import assigned_test, voice_test
from src.api.schemas.roster import AssignmentTarget
from src.api.schemas.voice_test import CreateTestIn
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    StudentProfile,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
    VoiceTestTargetStudent,
)


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_class_subject_chapter_topic(db_session, school_id):
    school_class = SchoolClass(school_id=school_id, grade=8, section=uuid.uuid4().hex[:8].upper())
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="Topic 1")
    db_session.add(topic)
    await db_session.flush()
    return school_class, subject, chapter, topic


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


async def test_whole_class_target_resolves_current_roster_at_creation_time(db_session):
    school = await _seed_school(db_session)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, school.id)
    teacher = await _seed_teacher(db_session, school.id)
    db_session.add(TeacherClassAssignment(teacher_id=teacher.id, class_id=school_class.id, subject_id=subject.id))
    # a Super Admin has given the school this subject
    db_session.add(SchoolCurriculum(school_id=school_class.school_id, subject_id=subject.id, enabled=True))
    student_a = await _seed_student(db_session, school.id, school_class.id, roll_number=1)
    student_b = await _seed_student(db_session, school.id, school_class.id, roll_number=2)
    await db_session.flush()

    body = CreateTestIn(
        class_id=str(school_class.id), subject_id=str(subject.id), chapter_id=str(chapter.id),
        topic_id=str(topic.id), bloom_levels=[BloomLevel.UNDERSTAND], duration_minutes=20,
        completion_window_hours=48, target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS),
    )
    result = await voice_test.create_test(body, background_tasks=BackgroundTasks(), user=teacher, session=db_session)

    assert result.assigned_count == 2

    delivered = await db_session.execute(
        select(VoiceTestTargetStudent.student_id).where(VoiceTestTargetStudent.test_id == result.id)
    )
    delivered_ids = {str(row[0]) for row in delivered.all()}
    assert delivered_ids == {str(student_a.id), str(student_b.id)}

    # still a DRAFT - students only see it once the teacher schedules it
    my_tests = await assigned_test.list_my_tests(user=student_a, session=db_session)
    assert result.id not in {t.id for t in my_tests.items}
    await voice_test.schedule_test(result.id, user=teacher, _feature=teacher, session=db_session)

    # The real end-to-end regression check: each student's own
    # "my assigned tests" read path (what nool-apps' student side actually
    # calls) must now return this test - before the fix, it returned
    # nothing for a WHOLE_CLASS test, for any student.
    for student in (student_a, student_b):
        my_tests = await assigned_test.list_my_tests(user=student, session=db_session)
        assert result.id in {t.id for t in my_tests.items}


async def test_specific_students_target_still_only_delivers_to_the_picked_students(db_session):
    school = await _seed_school(db_session)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, school.id)
    teacher = await _seed_teacher(db_session, school.id)
    db_session.add(TeacherClassAssignment(teacher_id=teacher.id, class_id=school_class.id, subject_id=subject.id))
    # a Super Admin has given the school this subject
    db_session.add(SchoolCurriculum(school_id=school_class.school_id, subject_id=subject.id, enabled=True))
    picked = await _seed_student(db_session, school.id, school_class.id, roll_number=1)
    not_picked = await _seed_student(db_session, school.id, school_class.id, roll_number=2)
    await db_session.flush()

    body = CreateTestIn(
        class_id=str(school_class.id), subject_id=str(subject.id), chapter_id=str(chapter.id),
        topic_id=str(topic.id), bloom_levels=[BloomLevel.UNDERSTAND], duration_minutes=20,
        completion_window_hours=48,
        target=AssignmentTarget(mode=AssignmentTargetMode.SPECIFIC_STUDENTS, student_ids=[str(picked.id)]),
    )
    result = await voice_test.create_test(body, background_tasks=BackgroundTasks(), user=teacher, session=db_session)

    assert result.assigned_count == 1
    await voice_test.schedule_test(result.id, user=teacher, _feature=teacher, session=db_session)

    picked_tests = await assigned_test.list_my_tests(user=picked, session=db_session)
    assert result.id in {t.id for t in picked_tests.items}

    not_picked_tests = await assigned_test.list_my_tests(user=not_picked, session=db_session)
    assert result.id not in {t.id for t in not_picked_tests.items}
