"""GET /me/journey - the "Journey" chapter map. Real rows against the dev
Postgres via db_session, same pattern as test_progress.py.
"""

import uuid
from datetime import datetime, timezone

from shared.errors import NotFoundError

from src.api.routes import journey
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Role,
    School,
    SchoolClass,
    StudentChapterProgress,
    Subject,
    User,
    UserStatus,
    VoiceTest,
)
from src.services.test_completion import record_test_completion


async def _seed_school(db_session) -> School:
    school = School(
        name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com"
    )
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_student(db_session, school_id) -> User:
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
    return student


async def _seed_subject_with_chapters(db_session, names: list[str]) -> tuple[Subject, list[Chapter]]:
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    chapters = []
    for index, name in enumerate(names):
        chapter = Chapter(subject_id=subject.id, name=name, order_index=index)
        db_session.add(chapter)
        chapters.append(chapter)
    await db_session.flush()
    return subject, chapters


async def test_journey_raises_not_found_for_unknown_subject(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    try:
        await journey.get_my_journey(subject_id=str(uuid.uuid4()), user=student, session=db_session)
        assert False, "expected NotFoundError"
    except NotFoundError:
        pass


async def test_journey_with_no_progress_yet_has_only_the_first_chapter_current(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    subject, chapters = await _seed_subject_with_chapters(db_session, ["Motion", "Force", "Gravitation"])

    result = await journey.get_my_journey(subject_id=str(subject.id), user=student, session=db_session)

    states = [node.state for node in result.nodes]
    assert states == ["CURRENT", "LOCKED", "LOCKED"]
    assert [node.chapter_label for node in result.nodes] == ["Motion", "Force", "Gravitation"]


async def test_journey_marks_completed_chapters_done_and_unlocks_the_next(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    subject, chapters = await _seed_subject_with_chapters(db_session, ["Motion", "Force", "Gravitation"])

    db_session.add(
        StudentChapterProgress(
            student_id=student.id,
            chapter_id=chapters[0].id,
            stars=3,
            completed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    result = await journey.get_my_journey(subject_id=str(subject.id), user=student, session=db_session)

    assert result.nodes[0].state == "DONE"
    assert result.nodes[0].stars_earned == 3
    assert result.nodes[1].state == "CURRENT"
    assert result.nodes[2].state == "LOCKED"


async def test_completing_a_test_advances_that_chapter_on_the_real_journey_endpoint(db_session):
    """End-to-end through record_test_completion (the real write path a
    finished Voice Test drives) into GET /me/journey (the real read path)
    - confirms the two are actually wired together, not just each unit-
    tested against a hand-seeded StudentChapterProgress row.
    """
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    subject, chapters = await _seed_subject_with_chapters(db_session, ["Motion", "Force"])
    school_class = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
    db_session.add(school_class)
    await db_session.flush()
    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapters[0].id, topic_id=None,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(test)
    await db_session.flush()

    await record_test_completion(
        db_session, student_id=student.id, test_id=test.id,
        answered=5, total_questions=5, bloom_levels=[BloomLevel.REMEMBER],
    )
    await db_session.flush()

    result = await journey.get_my_journey(subject_id=str(subject.id), user=student, session=db_session)

    assert result.nodes[0].state == "DONE"
    assert result.nodes[0].stars_earned == 3  # 100% mastery
    assert result.nodes[1].state == "CURRENT"
