"""GET /me/progress (the Student app's "Progress"/report-card screen) -
previously several fields (trajectory, trajectory_gain_label,
next_best_focus) were hardcoded placeholders regardless of real data;
this is real, computed-at-read-time content now, same honesty standard
school_analytics.py's compute_school_analytics already holds itself to.
Real rows against the dev Postgres via db_session, same pattern as
test_analytics.py.
"""

import uuid

from sqlalchemy import select

from src.api.routes import progress
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Role,
    School,
    SchoolClass,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    TestStatus,
    User,
    UserStatus,
    VoiceTest,
)


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_student(db_session, school_id) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    return student


async def _seed_result(db_session, school_id, student_id, *, mastery_percent: int, bloom_scores: dict[BloomLevel, int]) -> None:
    school_class = SchoolClass(school_id=school_id, grade=9, section=uuid.uuid4().hex[:8].upper())
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()

    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48,
        target_mode=AssignmentTargetMode.WHOLE_CLASS, status=TestStatus.RESULTS_READY,
    )
    db_session.add(test)
    await db_session.flush()

    result = StudentTestResult(test_id=test.id, student_id=student_id, mastery_percent=mastery_percent)
    db_session.add(result)
    await db_session.flush()
    for level, percent in bloom_scores.items():
        db_session.add(
            StudentTestResultBloomScore(student_test_result_id=result.id, bloom_level=level, percent=percent)
        )
    await db_session.flush()


async def test_progress_with_no_results_yet_is_honest_not_fake(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    result = await progress.get_my_progress(user=student, session=db_session)

    assert all(b.percent is None for b in result.bloom_mastery)
    assert result.subject_summaries == []
    assert result.trajectory == [progress.TrajectoryPointOut(label="Now", mastery_percent=0)]
    assert result.trajectory_gain_label == "Not enough tests yet"
    assert result.next_best_focus.label == "Take your first test"


async def test_progress_next_best_focus_targets_the_weakest_assessed_bloom_level(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    await _seed_result(
        db_session, school.id, student.id, mastery_percent=70,
        bloom_scores={BloomLevel.REMEMBER: 90, BloomLevel.ANALYZE: 40},
    )

    result = await progress.get_my_progress(user=student, session=db_session)

    assert result.next_best_focus.label == "Analyze-level questions"
    assert "40%" in result.next_best_focus.message


async def test_progress_trajectory_reflects_real_mastery_percent(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    await _seed_result(db_session, school.id, student.id, mastery_percent=60, bloom_scores={BloomLevel.UNDERSTAND: 60})

    result = await progress.get_my_progress(user=student, session=db_session)

    assert len(result.trajectory) == 1
    assert result.trajectory[0].mastery_percent == 60


async def test_progress_lists_each_finished_test_with_its_chapter_and_score(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    await _seed_result(db_session, school.id, student.id, mastery_percent=64, bloom_scores={BloomLevel.REMEMBER: 64})
    await _seed_result(db_session, school.id, student.id, mastery_percent=30, bloom_scores={BloomLevel.APPLY: 30})

    result = await progress.get_my_progress(user=student, session=db_session)

    assert sorted(t.score_percent for t in result.subject_tests) == [30, 64]
    assert all(t.label == "Chapter 1" and t.test_id for t in result.subject_tests)
    assert all(t.test_type.startswith("Chat with Milo · ") for t in result.subject_tests)


async def test_a_finished_speak_with_milo_recheck_is_listed_in_results(db_session):
    """S-002: the homework re-check (an MCQ test) never reached Me > Results."""
    from datetime import datetime, timezone

    from src.domain.models import McqTest
    from src.services import student_progress

    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    await _seed_result(db_session, school.id, student.id, mastery_percent=40, bloom_scores={BloomLevel.APPLY: 40})
    test = (await db_session.execute(select(StudentTestResult).where(
        StudentTestResult.student_id == student.id))).scalar_one()
    now = datetime.now(timezone.utc)
    db_session.add(McqTest(student_id=student.id, voice_test_id=test.test_id, unlocks_at=now, completed_at=now,
                           score_percent=60, result={"growth": [{"key": "APPLY", "kind": "bloom", "afterCorrected": 47}]}))
    await db_session.flush()

    [subject] = await student_progress.by_subject(db_session, student)

    assert [(r["kind"], r["score_percent"]) for r in subject["results"]] == [("Re-check", 60), ("Speak with Milo", 40)]
    assert subject["results"][0]["test_id"] == str(test.test_id)  # S-002: the row opens the test's insights
