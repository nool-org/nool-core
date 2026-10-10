"""POST /me/homework/{id}/confirm-completion - before this fix,
StudentHomeworkProgress.status and the parent Homework's status/
completed_count were dead fields, never written anywhere: confirming Q&A
only ever touched qa_completed/confirmed_at, so GET /me/homework/current's
`!= COMPLETED` filter never actually excluded a finished Homework and the
teacher's Homework list never reflected real progress. Real rows against
the dev Postgres via db_session, same seed pattern as test_homework_assign.py.
"""

from fastapi import BackgroundTasks
import uuid

from sqlalchemy import select

from src.api.routes import homework, student_homework
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
    StudentHomeworkStatus,
    Subject,
    User,
    UserStatus,
    VoiceTest,
)


async def _seed_homework(db_session, *, assigned_count: int) -> Homework:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    school_class = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add_all([school_class, chapter])
    await db_session.flush()

    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(test)
    await db_session.flush()

    hw = Homework(
        test_id=test.id, class_id=school_class.id, gap_topic="Field direction", gap_mastery_percent=60,
        total_questions=5, difficulty=HomeworkDifficulty.MIXED, target_mode=AssignmentTargetMode.WHOLE_CLASS,
        status=HomeworkStatus.ASSIGNED, assigned_count=assigned_count,
    )
    db_session.add(hw)
    await db_session.flush()
    return hw


async def _seed_student_with_progress(db_session, hw: Homework) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    db_session.add(StudentHomeworkProgress(homework_id=hw.id, student_id=student.id))
    await db_session.flush()
    return student


async def test_confirm_completion_flips_progress_status_and_bumps_homework_to_in_progress(db_session):
    hw = await _seed_homework(db_session, assigned_count=2)
    student_a = await _seed_student_with_progress(db_session, hw)
    await _seed_student_with_progress(db_session, hw)  # student_b, still ASSIGNED

    await student_homework.confirm_completion(homework_id=str(hw.id), background_tasks=BackgroundTasks(), user=student_a, session=db_session)
    await db_session.flush()

    progress = (
        await db_session.execute(
            select(StudentHomeworkProgress).where(
                StudentHomeworkProgress.homework_id == hw.id, StudentHomeworkProgress.student_id == student_a.id
            )
        )
    ).scalar_one()
    assert progress.status == StudentHomeworkStatus.COMPLETED
    assert progress.qa_completed is True
    assert progress.confirmed_at is not None

    await db_session.refresh(hw)
    assert hw.status == HomeworkStatus.IN_PROGRESS  # one of two done, not all
    assert hw.completed_count == 1


async def test_confirm_completion_by_every_assigned_student_completes_the_homework(db_session):
    hw = await _seed_homework(db_session, assigned_count=1)
    student = await _seed_student_with_progress(db_session, hw)

    await student_homework.confirm_completion(homework_id=str(hw.id), background_tasks=BackgroundTasks(), user=student, session=db_session)
    await db_session.flush()
    await db_session.refresh(hw)

    assert hw.status == HomeworkStatus.COMPLETED
    assert hw.completed_count == 1


async def test_confirm_completion_is_idempotent_on_replay(db_session):
    hw = await _seed_homework(db_session, assigned_count=1)
    student = await _seed_student_with_progress(db_session, hw)

    await student_homework.confirm_completion(homework_id=str(hw.id), background_tasks=BackgroundTasks(), user=student, session=db_session)
    await db_session.flush()
    await student_homework.confirm_completion(homework_id=str(hw.id), background_tasks=BackgroundTasks(), user=student, session=db_session)
    await db_session.flush()
    await db_session.refresh(hw)

    assert hw.completed_count == 1
    assert hw.status == HomeworkStatus.COMPLETED


async def test_get_current_homework_stops_returning_a_completed_homework(db_session):
    hw = await _seed_homework(db_session, assigned_count=1)
    student = await _seed_student_with_progress(db_session, hw)

    before = await student_homework.get_current_homework(user=student, session=db_session)
    assert before.homework is not None
    assert before.homework.homework_id == str(hw.id)

    await student_homework.confirm_completion(homework_id=str(hw.id), background_tasks=BackgroundTasks(), user=student, session=db_session)
    await db_session.flush()

    after = await student_homework.get_current_homework(user=student, session=db_session)
    assert after.homework is None
