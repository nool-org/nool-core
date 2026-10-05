"""POST /homework/{id}/assign - previously wrote HomeworkTargetStudent for
SPECIFIC_STUDENTS only, and never wrote StudentHomeworkProgress at all for
any target mode - meaning GET /me/homework/current (which reads
StudentHomeworkProgress, not HomeworkTargetStudent) had nothing to find
for any student on any assigned Homework. Real rows against the dev
Postgres via db_session, same pattern as test_progress.py.
"""

import uuid

import pytest

from shared.errors import ConflictError, ForbiddenError, ValidationError

from src.api.routes import homework
from src.api.schemas.homework import AssignHomeworkIn
from src.api.schemas.roster import AssignmentTarget
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    Homework,
    HomeworkDifficulty,
    HomeworkStatus,
    Role,
    School,
    SchoolClass,
    StudentHomeworkProgress,
    StudentProfile,
    Subject,
    TeacherClassAssignment,
    User,
    UserStatus,
    VoiceTest,
)


async def _seed_school(db_session) -> School:
    school = School(
        name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com"
    )
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_teacher(db_session, school_id) -> User:
    teacher = User(
        firebase_uid=f"teacher-{uuid.uuid4()}",
        email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher",
        role=Role.TEACHER,
        school_id=school_id,
        status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    return teacher


async def _seed_student(db_session, school_id, class_id) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}",
        email=f"{uuid.uuid4()}@example.com",
        display_name="Student",
        role=Role.STUDENT,
        school_id=school_id,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    # roll_number is unique per (class_id, roll_number) - derive one from
    # the generated uuid rather than a shared counter, so tests seeding
    # multiple students in the same class never collide.
    roll_number = uuid.uuid4().int % 100000
    db_session.add(StudentProfile(user_id=student.id, class_id=class_id, roll_number=roll_number))
    await db_session.flush()
    return student


async def _seed_homework(db_session, school_id, teacher) -> tuple[Homework, "SchoolClass"]:
    school_class = SchoolClass(school_id=school_id, grade=9, section=uuid.uuid4().hex[:8].upper())
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()

    test = VoiceTest(
        class_id=school_class.id,
        subject_id=subject.id,
        chapter_id=chapter.id,
        topic_id=None,
        duration_minutes=20,
        completion_window_hours=48,
        target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(test)
    # the assigning teacher teaches this class + subject (every homework route checks it)
    db_session.add(TeacherClassAssignment(teacher_id=teacher.id, class_id=school_class.id, subject_id=subject.id))
    await db_session.flush()

    hw = Homework(
        test_id=test.id,
        class_id=school_class.id,
        gap_topic="Field direction",
        gap_mastery_percent=60,
        total_questions=5,
        difficulty=HomeworkDifficulty.MIXED,
        target_mode=AssignmentTargetMode.WHOLE_CLASS,
        status=HomeworkStatus.REVIEW,
    )
    db_session.add(hw)
    await db_session.flush()
    return hw, school_class


async def test_assign_whole_class_creates_progress_for_every_roster_student(db_session):
    school = await _seed_school(db_session)
    teacher = await _seed_teacher(db_session, school.id)
    hw, school_class = await _seed_homework(db_session, school.id, teacher)
    student_a = await _seed_student(db_session, school.id, school_class.id)
    student_b = await _seed_student(db_session, school.id, school_class.id)

    result = await homework.assign_homework(
        homework_id=str(hw.id),
        body=AssignHomeworkIn(
            target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS),
            completion_window_hours=48,
        ),
        user=teacher,
        session=db_session,
    )

    assert result.assigned_count == 2
    assert result.status == HomeworkStatus.ASSIGNED

    progress = await db_session.execute(
        homework.select(StudentHomeworkProgress).where(StudentHomeworkProgress.homework_id == hw.id)
    )
    progressed_student_ids = {row.student_id for row in progress.scalars().all()}
    assert progressed_student_ids == {student_a.id, student_b.id}


async def test_assign_specific_students_creates_progress_only_for_targets(db_session):
    school = await _seed_school(db_session)
    teacher = await _seed_teacher(db_session, school.id)
    hw, school_class = await _seed_homework(db_session, school.id, teacher)
    targeted = await _seed_student(db_session, school.id, school_class.id)
    not_targeted = await _seed_student(db_session, school.id, school_class.id)

    result = await homework.assign_homework(
        homework_id=str(hw.id),
        body=AssignHomeworkIn(
            target=AssignmentTarget(
                mode=AssignmentTargetMode.SPECIFIC_STUDENTS, student_ids=[str(targeted.id)]
            ),
            completion_window_hours=48,
        ),
        user=teacher,
        session=db_session,
    )

    assert result.assigned_count == 1

    progress = await db_session.execute(
        homework.select(StudentHomeworkProgress).where(StudentHomeworkProgress.homework_id == hw.id)
    )
    progressed_student_ids = {row.student_id for row in progress.scalars().all()}
    assert progressed_student_ids == {targeted.id}
    assert not_targeted.id not in progressed_student_ids


async def test_assign_is_idempotent_on_replay(db_session):
    """Re-assigning (e.g. a teacher re-submitting) must not violate the
    (homework_id, student_id) unique constraint on StudentHomeworkProgress.
    """
    school = await _seed_school(db_session)
    teacher = await _seed_teacher(db_session, school.id)
    hw, school_class = await _seed_homework(db_session, school.id, teacher)
    await _seed_student(db_session, school.id, school_class.id)

    body = AssignHomeworkIn(
        target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS),
        completion_window_hours=48,
    )
    result = await homework.assign_homework(homework_id=str(hw.id), body=body, user=teacher, session=db_session)
    assert result.assigned_count == 1
    # a replay no longer re-runs the assignment (only REVIEW homework can be assigned) - no 500, no duplicates
    with pytest.raises(ConflictError):
        await homework.assign_homework(homework_id=str(hw.id), body=body, user=teacher, session=db_session)


async def test_assign_rejects_students_outside_the_class_and_teachers_who_dont_teach_it(db_session):
    school = await _seed_school(db_session)
    teacher = await _seed_teacher(db_session, school.id)
    other_teacher = await _seed_teacher(db_session, school.id)
    hw, school_class = await _seed_homework(db_session, school.id, teacher)
    other_school = await _seed_school(db_session)
    outsider_class = SchoolClass(school_id=other_school.id, grade=9, section="Z")
    db_session.add(outsider_class)
    await db_session.flush()
    outsider = await _seed_student(db_session, other_school.id, outsider_class.id)
    specific = AssignHomeworkIn(
        target=AssignmentTarget(mode=AssignmentTargetMode.SPECIFIC_STUDENTS, student_ids=[str(outsider.id)]),
        completion_window_hours=48,
    )
    with pytest.raises(ValidationError):
        await homework.assign_homework(homework_id=str(hw.id), body=specific, user=teacher, session=db_session)
    with pytest.raises(ForbiddenError):
        await homework.assign_homework(homework_id=str(hw.id), body=specific, user=other_teacher, session=db_session)
