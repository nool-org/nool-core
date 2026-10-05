"""homework_autogen.auto_generate_homework - the per-student automatic
follow-up Homework fanned out from test_completion.py. Exercised directly
here (rather than only indirectly through test_test_completion.py) so the
no-dataset no-op path is deterministic regardless of what else is seeded
in the shared dev Postgres.
"""

import uuid

from sqlalchemy import select

from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Dataset,
    Homework,
    HomeworkQuestion,
    HomeworkStatus,
    HomeworkTargetStudent,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    SchoolDataset,
    StudentProfile,
    StudentHomeworkProgress,
    Subject,
    User,
    UserStatus,
    VoiceTest,
)
from src.services import homework_autogen
from src.services.homework_autogen import AUTO_HOMEWORK_QUESTION_COUNT, auto_generate_homework


async def _seed_test(db_session) -> VoiceTest:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}", grade=9)
    db_session.add_all([school, subject])
    await db_session.flush()
    school_class = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Photosynthesis")
    db_session.add_all([school_class, chapter])
    await db_session.flush()
    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add_all([test, SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db_session.flush()
    return test


async def _seed_student(db_session, test: VoiceTest) -> User:
    school_class = await db_session.get(SchoolClass, test.class_id)
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=school_class.school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    db_session.add(StudentProfile(user_id=student.id, class_id=school_class.id, roll_number=uuid.uuid4().int % 100000))
    await db_session.flush()
    return student


async def _assign(db_session, test: VoiceTest, dataset: Dataset) -> None:
    school_class = await db_session.get(SchoolClass, test.class_id)
    db_session.add(SchoolDataset(school_id=school_class.school_id, dataset_id=dataset.id, enabled=True))
    await db_session.flush()


async def test_creates_homework_targeted_at_just_this_student_using_the_subjects_dataset(db_session):
    test = await _seed_test(db_session)
    student = await _seed_student(db_session, test)
    dataset = Dataset(name="Bank", question_count=20, description="", subject_id=test.subject_id, grade=9)
    db_session.add(dataset)
    await db_session.flush()
    await _assign(db_session, test, dataset)

    await auto_generate_homework(
        db_session, test=test, student_id=student.id, mastery_percent=60,
        bloom_levels=[BloomLevel.REMEMBER, BloomLevel.APPLY],
    )
    await db_session.flush()

    hw = (await db_session.execute(select(Homework).where(Homework.test_id == test.id))).scalar_one()
    assert hw.status == HomeworkStatus.ASSIGNED
    assert hw.target_mode == AssignmentTargetMode.SPECIFIC_STUDENTS
    assert hw.gap_topic == "Photosynthesis"
    assert hw.gap_mastery_percent == 60
    assert hw.assigned_count == 1

    progress = (
        await db_session.execute(
            select(StudentHomeworkProgress).where(StudentHomeworkProgress.homework_id == hw.id)
        )
    ).scalar_one()
    assert progress.student_id == student.id

    target = (
        await db_session.execute(
            select(HomeworkTargetStudent).where(HomeworkTargetStudent.homework_id == hw.id)
        )
    ).scalar_one()
    assert target.student_id == student.id

    questions = (
        await db_session.execute(select(HomeworkQuestion).where(HomeworkQuestion.homework_id == hw.id))
    ).scalars().all()
    assert len(questions) == AUTO_HOMEWORK_QUESTION_COUNT
    assert all(q.dataset_id == dataset.id for q in questions)


async def test_never_grounds_on_a_catalog_dataset_the_school_wasnt_assigned(db_session):
    test = await _seed_test(db_session)
    student = await _seed_student(db_session, test)
    db_session.add(Dataset(name="Unassigned", question_count=5, description="", subject_id=test.subject_id, grade=9))
    await db_session.flush()

    await auto_generate_homework(
        db_session, test=test, student_id=student.id, mastery_percent=40, bloom_levels=[]
    )
    await db_session.flush()

    assert (await db_session.execute(select(Homework).where(Homework.test_id == test.id))).first() is None


async def _no_dataset(*_args, **_kwargs):
    return None


async def test_no_ops_when_no_dataset_exists_anywhere(db_session, monkeypatch):
    test = await _seed_test(db_session)
    student = await _seed_student(db_session, test)
    monkeypatch.setattr(homework_autogen, "_pick_dataset", _no_dataset)

    await auto_generate_homework(
        db_session, test=test, student_id=student.id, mastery_percent=40, bloom_levels=[]
    )
    await db_session.flush()

    assert (
        await db_session.execute(select(Homework).where(Homework.test_id == test.id))
    ).scalar_one_or_none() is None


async def test_a_failing_background_homework_build_is_contained(monkeypatch):
    """schedule_auto_homework runs after the results are committed; if the
    build blows up (LLM/network/anything) it's logged and swallowed - it can
    never surface to (or roll back) the completed test."""
    import asyncio
    import contextlib

    class FakeSession:
        async def get(self, *_):
            return object()  # "the test"

        async def commit(self):
            raise AssertionError("must not commit a failed build")

    @contextlib.asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    async def boom(*_a, **_k):
        raise RuntimeError("generation service exploded")

    monkeypatch.setattr("src.repositories.get_session", fake_get_session)
    monkeypatch.setattr(homework_autogen, "auto_generate_homework", boom)
    homework_autogen.schedule_auto_homework(test_id=uuid.uuid4(), student_id=uuid.uuid4(), mastery_percent=40,
                                            bloom_levels=[BloomLevel.REMEMBER])
    tasks = list(homework_autogen._background)
    assert tasks
    await asyncio.gather(*tasks)   # completes without raising
    assert all(t.exception() is None for t in tasks)
