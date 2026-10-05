"""GET /me/gamification and POST /me/gamification/award - the Student
Home Level hub. Real rows against the dev Postgres via db_session, same
pattern as test_progress.py.
"""

import uuid
from datetime import datetime, timezone

from src.api.routes import gamification
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    Role,
    School,
    SchoolClass,
    StudentChapterProgress,
    StudentTestResult,
    Subject,
    TestStatus,
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


async def test_gamification_creates_state_lazily_at_level_one(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    result = await gamification.get_my_gamification(user=student, session=db_session)

    assert result.level == 1
    assert result.xp == 0
    assert result.xp_for_next_level == 500


async def test_gamification_marks_open_app_goal_complete_just_by_reading(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    result = await gamification.get_my_gamification(user=student, session=db_session)

    open_app = next(g for g in result.daily_goals if g.id == "goal-open-app")
    assert open_app.completed is True
    finish_voice_test = next(g for g in result.daily_goals if g.id == "goal-finish-voice-test")
    assert finish_voice_test.completed is False


async def test_award_session_xp_accumulates_and_marks_goal_complete(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    body = gamification.AwardSessionXpIn(xp_earned=50, completed_goal_id="goal-finish-voice-test")
    result = await gamification.award_session_xp(body=body, user=student, session=db_session)

    assert result.xp == 50
    assert result.level == 1
    finish_voice_test = next(g for g in result.daily_goals if g.id == "goal-finish-voice-test")
    assert finish_voice_test.completed is True


async def test_award_session_xp_rolls_a_level_over(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    # the client-reported award is capped at SESSION_XP (anti-minting), so
    # roll-over is exercised through apply_xp, which server-side awards use
    state = await gamification.apply_xp(db_session, student.id, 520)

    assert state.level == 2
    assert state.xp == 20


async def test_award_session_xp_caps_the_client_reported_amount(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    body = gamification.AwardSessionXpIn(xp_earned=520)
    result = await gamification.award_session_xp(body=body, user=student, session=db_session)

    assert result.level == 1
    assert result.xp == gamification.SESSION_XP


async def test_achievements_unlock_from_real_rows(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    school_class = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
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
        status=TestStatus.RESULTS_READY,
    )
    db_session.add(test)
    await db_session.flush()
    db_session.add(StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=92))
    await db_session.flush()

    result = await gamification.get_my_gamification(user=student, session=db_session)

    mastery_achievement = next(a for a in result.achievements if a.id == "mastery-90")
    assert mastery_achievement.unlocked is True
    chapter_achievement = next(a for a in result.achievements if a.id == "first-chapter-complete")
    assert chapter_achievement.unlocked is False  # StudentTestResult alone doesn't set StudentChapterProgress


async def test_first_chapter_complete_achievement_unlocks_from_student_chapter_progress(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    db_session.add(
        StudentChapterProgress(
            student_id=student.id, chapter_id=chapter.id, stars=2,
            completed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    result = await gamification.get_my_gamification(user=student, session=db_session)

    chapter_achievement = next(a for a in result.achievements if a.id == "first-chapter-complete")
    assert chapter_achievement.unlocked is True
