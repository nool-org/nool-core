"""src/services/practice_bank.py's archive_homework_questions and
GET /me/practice-bank - real rows against the dev Postgres via db_session,
same pattern as test_homework_confirm_completion.py.
"""

import uuid

from src.api.routes import practice_bank, student_homework
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Dataset,
    Homework,
    HomeworkDifficulty,
    HomeworkQuestion,
    HomeworkStatus,
    PracticeBankEntry,
    Role,
    School,
    SchoolClass,
    StudentHomeworkProgress,
    Subject,
    User,
    UserStatus,
    VoiceTest,
)
from src.services.practice_bank import archive_homework_questions
from sqlalchemy import select


async def _seed_homework_with_questions(db_session, *, question_count=3) -> tuple[Homework, VoiceTest]:
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
        total_questions=question_count, difficulty=HomeworkDifficulty.MIXED,
        target_mode=AssignmentTargetMode.WHOLE_CLASS, status=HomeworkStatus.ASSIGNED, assigned_count=1,
    )
    db_session.add(hw)
    await db_session.flush()
    dataset = Dataset(name="Bank", question_count=10, description="", subject_id=subject.id)
    db_session.add(dataset)
    await db_session.flush()
    for order in range(1, question_count + 1):
        db_session.add(
            HomeworkQuestion(
                homework_id=hw.id, order=order, bloom_level=BloomLevel.UNDERSTAND,
                dataset_id=dataset.id, text=f"Question {order}", answer=f"Answer {order}",
            )
        )
    await db_session.flush()
    return hw, test


async def _seed_student(db_session) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    return student


async def test_archive_homework_questions_copies_every_question_with_denormalized_context(db_session):
    hw, test = await _seed_homework_with_questions(db_session, question_count=3)
    student = await _seed_student(db_session)

    await archive_homework_questions(db_session, homework=hw, student_id=student.id)
    await db_session.flush()

    rows = (
        await db_session.execute(select(PracticeBankEntry).where(PracticeBankEntry.student_id == student.id))
    ).scalars().all()
    assert len(rows) == 3
    assert all(r.source_homework_id == hw.id for r in rows)
    assert all(r.subject_id == test.subject_id for r in rows)
    assert all(r.chapter_id == test.chapter_id for r in rows)
    assert {r.text for r in rows} == {"Question 1", "Question 2", "Question 3"}
    assert {r.answer for r in rows} == {"Answer 1", "Answer 2", "Answer 3"}


async def test_archive_homework_questions_is_idempotent_on_replay(db_session):
    hw, _test = await _seed_homework_with_questions(db_session, question_count=2)
    student = await _seed_student(db_session)

    await archive_homework_questions(db_session, homework=hw, student_id=student.id)
    await db_session.flush()
    await archive_homework_questions(db_session, homework=hw, student_id=student.id)
    await db_session.flush()

    rows = (
        await db_session.execute(select(PracticeBankEntry).where(PracticeBankEntry.student_id == student.id))
    ).scalars().all()
    assert len(rows) == 2


async def test_confirm_completion_archives_the_homeworks_questions(db_session):
    hw, _test = await _seed_homework_with_questions(db_session, question_count=2)
    student = await _seed_student(db_session)
    db_session.add(StudentHomeworkProgress(homework_id=hw.id, student_id=student.id))
    await db_session.flush()

    await student_homework.confirm_completion(homework_id=str(hw.id), user=student, session=db_session)
    await db_session.flush()

    rows = (
        await db_session.execute(select(PracticeBankEntry).where(PracticeBankEntry.student_id == student.id))
    ).scalars().all()
    assert len(rows) == 2


async def test_list_my_practice_bank_scopes_to_the_caller_and_supports_filters(db_session):
    hw, test = await _seed_homework_with_questions(db_session, question_count=2)
    student = await _seed_student(db_session)
    other_student = await _seed_student(db_session)

    await archive_homework_questions(db_session, homework=hw, student_id=student.id)
    await archive_homework_questions(db_session, homework=hw, student_id=other_student.id)
    await db_session.flush()

    result = await practice_bank.list_my_practice_bank(
        subject_id=None, chapter_id=None, bloom_level=None, user=student, session=db_session
    )
    assert result.total == 2
    assert all(item.subject_id == str(test.subject_id) for item in result.items)

    filtered = await practice_bank.list_my_practice_bank(
        subject_id=str(test.subject_id), chapter_id=None,
        bloom_level=BloomLevel.APPLY, user=student, session=db_session,
    )
    assert filtered.total == 0  # seeded questions are all UNDERSTAND, not APPLY

    other_result = await practice_bank.list_my_practice_bank(
        subject_id=None, chapter_id=None, bloom_level=None, user=other_student, session=db_session
    )
    assert other_result.total == 2
    assert {item.id for item in other_result.items} != {item.id for item in result.items}
