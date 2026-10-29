"""GET /me/gamification and POST /me/gamification/award - the Student
Home Level hub. Real rows against the dev Postgres via db_session, same
pattern as test_progress.py.
"""

import asyncio
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from src.api.routes import gamification
from src.domain.models.gamification import StudentBadge
from src.services import gamification as gamification_service
from src.domain.models import (
    AiAssessorSession,
    AssignmentTargetMode,
    BloomLevel,
    Homework,
    HomeworkDifficulty,
    StudentHomeworkProgress,
    StudentProfile,
    StudentTestResultBloomScore,
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

REVISION = "c7badbe03a03"


async def _ddl(db, work, attempts=5):
    """Runs `work` (sync, gets the connection) in a savepoint, retried on
    deadlock / lock timeout (same as test_voice_test_questions.py)."""
    for attempt in range(attempts):
        savepoint = await db.begin_nested()
        try:
            await db.execute(text("SET LOCAL lock_timeout = '3s'"))
            await db.run_sync(lambda s: work(s.connection()))
            await savepoint.commit()
            return
        except DBAPIError as exc:
            await savepoint.rollback()
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code not in ("40P01", "55P03") or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


def _create_badges_table(conn):
    if not inspect(conn).has_table(StudentBadge.__tablename__):
        conn.execute(CreateTable(StudentBadge.__table__, include_foreign_key_constraints=[]))


@pytest_asyncio.fixture
async def db_session(db_session):
    """Until the dev DB has this item's migration, creates student_badges
    inside the test's transaction (no FK, so it never locks `users`)."""
    await _ddl(db_session, _create_badges_table)
    return db_session


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

    body = gamification.AwardSessionXpIn(completed_goal_id="goal-open-app")
    result = await gamification.award_session_xp(body=body, user=student, session=db_session)
    again = await gamification.award_session_xp(body=body, user=student, session=db_session)

    assert result.xp == 10 and again.xp == 10  # the goal's own reward, once a day
    open_app = next(g for g in result.daily_goals if g.id == "goal-open-app")
    assert open_app.completed is True
    finish_voice_test = next(g for g in result.daily_goals if g.id == "goal-finish-voice-test")
    assert finish_voice_test.completed is False


async def test_award_session_xp_rolls_a_level_over(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    # the client can no longer award XP, so
    # roll-over is exercised through apply_xp, which server-side awards use
    state = await gamification.apply_xp(db_session, student.id, 520)

    assert state.level == 2
    assert state.xp == 20


async def test_award_session_xp_ignores_the_clients_amount(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)

    body = gamification.AwardSessionXpIn.model_validate({"xpEarned": 520, "completedGoalId": "goal-finish-voice-test"})
    result = await gamification.award_session_xp(body=body, user=student, session=db_session)
    await gamification.award_session_xp(body=gamification.AwardSessionXpIn.model_validate({"xpEarned": -100}),
                                        user=student, session=db_session)

    assert result.level == 1 and result.xp == 0
    assert next(g for g in result.daily_goals if g.id == "goal-finish-voice-test").completed is False


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


# ------------------------------------------------------------- migration
def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_student_badges.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w3_03_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "c7e2a9d4f1b3"  # chained after W3-09 at merge

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                getattr(module, step)()
        return work

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        if not insp.has_table("student_badges"):
            return None
        return {c["name"] for c in insp.get_columns("student_badges")}

    # drop what the autouse fixture (or a migrated dev DB) has - rolled back after the test
    await _ddl(db_session, lambda conn: conn.execute(text("DROP TABLE IF EXISTS student_badges")))
    await _ddl(db_session, run("upgrade"))
    assert await db_session.run_sync(columns) == {c.name for c in StudentBadge.__table__.columns}
    await _ddl(db_session, run("downgrade"))
    assert await db_session.run_sync(columns) is None


# ---------------------------------------------------------------- badges
async def _seed_class_test(db_session, school, status=TestStatus.RESULTS_READY) -> tuple[SchoolClass, VoiceTest]:
    school_class = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    test = VoiceTest(class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, duration_minutes=10,
                     completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS, status=status)
    db_session.add(test)
    await db_session.flush()
    return school_class, test


async def _result(db_session, test, student, scores: dict, session_started=None) -> StudentTestResult:
    result = StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=70)
    db_session.add(result)
    await db_session.flush()
    db_session.add_all([StudentTestResultBloomScore(student_test_result_id=result.id, bloom_level=level, percent=p)
                        for level, p in scores.items()])
    if session_started:
        db_session.add(AiAssessorSession(student_id=student.id, test_id=test.id, context_label="t",
                                         duration_seconds=60, created_at=session_started))
    await db_session.flush()
    return result


async def test_badges_earned_at_80_percent_and_persist_through_regrade(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    _, test = await _seed_class_test(db_session, school)
    started = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    result = await _result(db_session, test, student, {
        BloomLevel.UNDERSTAND: 80, BloomLevel.APPLY: 79, BloomLevel.ANALYZE: None, BloomLevel.REMEMBER: 100,
    }, session_started=started)

    out = await gamification.get_my_gamification(user=student, session=db_session)

    badges = {b.id: b for b in out.badges}
    assert [b.name for b in out.badges] == ["Explainer", "Problem Solver", "Detective", "Judge"]
    assert badges["badge-explainer"].earned and badges["badge-explainer"].earned_at == started
    assert badges["badge-explainer"].bloom_level == BloomLevel.UNDERSTAND
    assert not badges["badge-problem-solver"].earned and badges["badge-problem-solver"].earned_at is None
    assert not badges["badge-detective"].earned

    # a regrade drops the score - the badge stays
    score = await db_session.scalar(select(StudentTestResultBloomScore).where(
        StudentTestResultBloomScore.student_test_result_id == result.id,
        StudentTestResultBloomScore.bloom_level == BloomLevel.UNDERSTAND))
    score.percent = 40
    await db_session.flush()
    out = await gamification.get_my_gamification(user=student, session=db_session)
    assert next(b for b in out.badges if b.id == "badge-explainer").earned_at == started
    rows = (await db_session.execute(select(StudentBadge).where(StudentBadge.student_id == student.id))).scalars().all()
    assert [r.badge_id for r in rows] == ["badge-explainer"]

    dumped = out.model_dump(by_alias=True)
    assert set(dumped["badges"][0]) == {"id", "name", "bloomLevel", "earned", "earnedAt"}
    assert {"level", "xp", "xpForNextLevel", "dailyGoals", "achievements", "badges", "quests"} <= set(dumped)


async def test_badge_earned_at_is_the_first_qualifying_session(db_session):
    school = await _seed_school(db_session)
    student = await _seed_student(db_session, school.id)
    _, later_test = await _seed_class_test(db_session, school)
    _, first_test = await _seed_class_test(db_session, school)
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    await _result(db_session, later_test, student, {BloomLevel.EVALUATE: 95}, session_started=first + timedelta(days=9))
    await _result(db_session, first_test, student, {BloomLevel.EVALUATE: 85}, session_started=first)

    earned = await gamification_service.sync_badges(db_session, student.id)

    assert earned == {"badge-judge": first}


# ---------------------------------------------------------------- quests
async def _classmate(db_session, school, school_class, roll) -> User:
    student = await _seed_student(db_session, school.id)
    db_session.add(StudentProfile(user_id=student.id, class_id=school_class.id, roll_number=roll))
    await db_session.flush()
    return student


async def test_class_quests_track_voice_test_and_homework_this_week(db_session):
    school = await _seed_school(db_session)
    school_class, test = await _seed_class_test(db_session, school)
    students = [await _classmate(db_session, school, school_class, roll) for roll in range(1, 6)]  # target 4
    for s in students[:2]:
        db_session.add(StudentTestResult(test_id=test.id, student_id=s.id, mastery_percent=60))
    homework = Homework(test_id=test.id, class_id=school_class.id, gap_topic="x", gap_mastery_percent=40,
                        total_questions=5, difficulty=HomeworkDifficulty.MIXED,
                        target_mode=AssignmentTargetMode.WHOLE_CLASS)
    db_session.add(homework)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        StudentHomeworkProgress(homework_id=homework.id, student_id=students[0].id, confirmed_at=now),
        StudentHomeworkProgress(homework_id=homework.id, student_id=students[1].id, confirmed_at=now),
        StudentHomeworkProgress(homework_id=homework.id, student_id=students[2].id,
                                confirmed_at=now - timedelta(days=8)),  # last week
        StudentHomeworkProgress(homework_id=homework.id, student_id=students[3].id),  # not done
    ])
    await db_session.flush()

    out = await gamification.get_my_gamification(user=students[4], session=db_session)

    voice, homework_quest = out.quests
    assert (voice.id, voice.progress, voice.target, voice.scope) == (f"quest-voice-test-{test.id}", 2, 4, "class")
    assert homework_quest.id.startswith("quest-homework-")
    assert (homework_quest.progress, homework_quest.target) == (10, 50)
    assert set(out.model_dump(by_alias=True)["quests"][0]) == {"id", "title", "progress", "target", "scope"}


async def test_class_quests_skip_draft_and_old_tests_and_classless_students(db_session):
    school = await _seed_school(db_session)
    school_class, test = await _seed_class_test(db_session, school, status=TestStatus.DRAFT)
    student = await _classmate(db_session, school, school_class, 1)

    assert [q["id"][:15] for q in await gamification_service.class_quests(db_session, student.id)] == [
        "quest-homework-"]

    test.status = TestStatus.ACTIVE
    await db_session.flush()
    assert len(await gamification_service.class_quests(db_session, student.id)) == 2
    next_month = datetime.now(timezone.utc) + timedelta(days=30)
    assert len(await gamification_service.class_quests(db_session, student.id, now=next_month)) == 1

    loner = await _seed_student(db_session, school.id)
    assert await gamification_service.class_quests(db_session, loner.id) == []


def test_week_starts_monday_midnight_ist():
    # Sunday 20:00 UTC = Monday 01:30 IST -> that Monday
    start = gamification_service._week_start(datetime(2026, 10, 4, 20, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-10-05T00:00:00+05:30"


# ------------------------------------------------------------------ boss
def test_boss_concept_is_the_lowest_scoring_answers_concept():
    report = {"qa_transcripts": [
        {"question_number": 1, "score_percent": 70},
        {"question_number": 2, "score_percent": 20},
        {"question_number": 3, "score_percent": 20},
        {"question_number": 4, "score_percent": 5},  # no concepts -> ignored
        {"question_number": 5},  # unscored
    ]}
    concepts = {1: ["current"], 2: ["resistance", "ohm"], 3: ["power"], 4: [], 5: ["voltage"]}

    assert gamification_service.boss_concept_for(report, concepts) == "resistance"
    assert gamification_service.boss_concept_for(None, concepts) is None
    assert gamification_service.boss_concept_for({"qa_transcripts": []}, concepts) is None
