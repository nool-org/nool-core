"""C5: the follow-up MCQ test and Bloom growth - unlock rule, question
composition (bank first, generation for gaps, relaxed bank when generation
is down), no answers in GET, marking and growth math, one attempt, teacher
growth and its authz, and the migration.

Real rows against the dev Postgres via db_session; until the dev DB has this
item's migration the `schema` fixture creates the two new tables inside the
test's own transaction, without foreign keys (see test_voice_test_questions.py).
"""

import asyncio
import importlib.util
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from src.api.routes import mcq_test as routes
from src.api.schemas.mcq_test import LifelineIn, McqAnswerIn, McqAttemptIn, SpeedModeIn
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Dataset,
    DatasetQuestion,
    Homework,
    HomeworkDifficulty,
    HomeworkStatus,
    McqQuestionAnswer,
    McqTest,
    McqTestQuestion,
    QuestionType,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    SchoolDataset,
    StudentGamification,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentPoints,
    StudentProfile,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestQuestion,
)
from src.services import mcq_test as service
from src.services.generation_client import GenerationServiceUnavailableError
from src.services.growth import corrected, growth_entries

B = BloomLevel
LEVELS = [B.REMEMBER, B.UNDERSTAND, B.APPLY, B.ANALYZE, B.EVALUATE]
NEW_TABLES = [McqTest.__table__, McqTestQuestion.__table__]
REVISION = "c5e7a2b9d413"


async def _ddl(db, work, attempts=5):
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


def _create_new_tables(conn):
    for table in NEW_TABLES:
        if not inspect(conn).has_table(table.name):
            conn.execute(CreateTable(table, include_foreign_key_constraints=[]))


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_new_tables)
    return db_session


# ------------------------------------------------------------------ seed
def _user(role, school_id, name="U"):
    return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=name,
                role=role, school_id=school_id, status=UserStatus.ACTIVE)


# voice-test scores per level (question order 1..5): weak current/resistance, strong power
SCORES = {1: 20, 2: 50, 3: 40, 4: 90, 5: 30}
CONCEPTS = {1: "current", 2: "current", 3: "resistance", 4: "power", 5: "resistance"}


async def _seed(db, *, taken_hours_ago=1) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    teacher, student = _user(Role.TEACHER, school.id), _user(Role.STUDENT, school.id, "Asha")
    db.add_all([section, chapter, teacher, student])
    await db.flush()
    topic = Topic(chapter_id=chapter.id, name="Ohm's law", text_content="V = IR. Current flows ...")
    dataset = Dataset(name="Important", question_count=0, description="", subject_id=subject.id, grade=10, board="CBSE")
    db.add_all([topic, dataset, StudentProfile(user_id=student.id, class_id=section.id, roll_number=1),
                TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db.flush()
    db.add(SchoolDataset(school_id=school.id, dataset_id=dataset.id, enabled=True))
    test = VoiceTest(class_id=section.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=topic.id,
                     duration_minutes=10, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS)
    db.add(test)
    await db.flush()
    for order, level in enumerate(LEVELS, start=1):
        db.add(VoiceTestQuestion(voice_test_id=test.id, order=order, bloom_level=level, topic_id=topic.id,
                                 concepts=[CONCEPTS[order]], original_text=f"q{order}", reworded_text=f"q{order}",
                                 spoken_text=f"q{order}", expected_points=[], hints=[],
                                 misconceptions=[f"{CONCEPTS[order]} misconception {order}"]))
    result = StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=46)
    db.add(result)
    await db.flush()
    db.add_all([StudentTestResultBloomScore(student_test_result_id=result.id, bloom_level=level, percent=SCORES[o])
                for o, level in enumerate(LEVELS, start=1)])
    sess = AiAssessorSession(student_id=student.id, test_id=test.id, context_label="t", duration_seconds=60,
                             created_at=datetime.now(timezone.utc) - timedelta(hours=taken_hours_ago))
    db.add(sess)
    await db.flush()
    db.add(AiAssessorSessionRecord(session_id=sess.id, report={"qa_transcripts": [
        {"question_number": o, "score_percent": s, "student_response": "x"} for o, s in SCORES.items()]}))
    await db.flush()
    return {"school": school, "teacher": teacher, "student": student, "test": test, "dataset": dataset,
            "chapter": chapter, "class": section}


async def _mcq(db, s, level, concept, n=0) -> DatasetQuestion:
    q = DatasetQuestion(dataset_id=s["dataset"].id, chapter_id=s["chapter"].id, bloom_level=level,
                        question_type=QuestionType.MCQ, text=f"{level.value} {concept} mcq {n}",
                        options=["w1", "right", "w2", "w3"], answer="right", created_by=s["teacher"].id,
                        external_uid=f"uid-{uuid.uuid4()}", concepts=[concept])
    db.add(q)
    await db.flush()
    return q


async def _full_bank(db, s):
    """2 per level on the concepts plan_slots asks for, plus power checks."""
    for level in LEVELS:
        for n, concept in enumerate(["current", "resistance", "power"]):
            await _mcq(db, s, level, concept, n)


async def _open(db, s):
    [item] = (await routes.list_my_mcq_tests(user=s["student"], session=db)).items
    return item


def _no_generation(monkeypatch):
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: False)


async def _get_test(mcq_test_id, *, user, session):
    """GET /me/mcq-tests/{id} as the app sees it once built: the first open answers
    "preparing" and builds in the background (here: built in this session first)."""
    from fastapi import BackgroundTasks

    first = await routes.get_my_mcq_test(mcq_test_id, BackgroundTasks(), user=user, session=session)
    if not first.preparing:
        return first
    mcq = await session.get(McqTest, uuid.UUID(str(mcq_test_id)))
    await routes.mcq_test.ensure_questions(session, mcq, user)
    return await routes.get_my_mcq_test(mcq_test_id, BackgroundTasks(), user=user, session=session)


async def test_the_first_open_answers_at_once_and_builds_in_the_background(schema, monkeypatch):
    from fastapi import BackgroundTasks

    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    item = await _open(db, s)
    tasks = BackgroundTasks()
    out = await routes.get_my_mcq_test(item.id, tasks, user=s["student"], session=db)
    assert out.preparing is True and out.questions == []
    assert [t.func for t in tasks.tasks] == [routes.mcq_test.prebuild]  # built off the request


# ---------------------------------------------------------------- unlock
async def test_fresh_result_is_locked_until_48h_after_the_test(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=1)
    item = await _open(db, s)
    assert item.status == "locked" and item.question_count == 10
    assert item.unlocks_at > datetime.now(timezone.utc) + timedelta(hours=46)
    with pytest.raises(ConflictError):
        await _get_test(item.id, user=s["student"], session=db)
    with pytest.raises(ConflictError):
        await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=[]), user=s["student"], session=db)


async def test_unlocks_48h_after_the_voice_test(schema):
    s = await _seed(schema, taken_hours_ago=49)
    item = await _open(schema, s)
    assert item.status == "available" and item.unlocks_at is None
    assert item.title == "Electricity · Ohm's law" and item.test_id == str(s["test"].id)


async def test_unlocks_when_the_tests_homework_is_confirmed(schema):
    db = schema
    s = await _seed(db, taken_hours_ago=1)
    assert (await _open(db, s)).status == "locked"
    hw = Homework(test_id=s["test"].id, class_id=s["class"].id, gap_topic="x", gap_mastery_percent=40,
                  total_questions=5, difficulty=HomeworkDifficulty.MIXED,
                  target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS, status=HomeworkStatus.COMPLETED)
    db.add(hw)
    await db.flush()
    db.add(StudentHomeworkProgress(homework_id=hw.id, student_id=s["student"].id,
                                   status=StudentHomeworkStatus.COMPLETED))
    await db.flush()
    assert (await _open(db, s)).status == "available"


# ----------------------------------------------------------- composition
def test_plan_two_per_level_weak_concepts_first_with_strong_checks():
    slots = service.plan_slots(LEVELS, {"current": 35, "resistance": 35, "power": 90})
    assert [lvl for lvl, _ in slots] == [lvl for lvl in LEVELS for _ in range(2)]
    concepts = [c for _, c in slots]
    assert concepts.count("power") == 1 and concepts[-1] == "power"
    assert set(concepts[:9]) == {"current", "resistance"}


def test_correct_index_needs_four_distinct_options():
    assert service.correct_index(["a", "b", "c", "d"], "c") == 2
    assert service.correct_index(["w", "x", "y", "z"], "(b)") == 1
    assert service.correct_index(["True", "False"], "True") is None
    assert service.correct_index(["a", "a", "c", "d"], "a") is None
    assert service.correct_index(["a", "b", "c", "d"], "e") is None


async def test_questions_come_from_the_bank_and_never_reveal_answers(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    item = await _open(db, s)
    out = await _get_test(item.id, user=s["student"], session=db)
    assert len(out.questions) == 10
    assert [q.bloom_level for q in out.questions] == [lvl.value for lvl in LEVELS for _ in range(2)]
    assert sum(q.concept == "power" for q in out.questions) == 1
    assert all(q.concept in ("current", "resistance") for q in out.questions if q.concept != "power")
    assert all(q.text.split()[1] == q.concept for q in out.questions)  # bank question on the slot's concept
    wire = out.model_dump(by_alias=True)
    assert wire["answered"] == [] and wire["speedMode"] is False and wire["lifelinesLeft"] == ["fifty_fifty", "ask_milo"]
    assert all(set(q) == {"id", "text", "options", "bloomLevel", "concept", "isBoss", "gapConcept", "figureSvg", "figureUrl"}
               for q in wire["questions"])
    assert [q.is_boss for q in out.questions] == [False] * 9 + [True]
    assert all(q.gap_concept == (q.concept if q.concept != "power" else None) for q in out.questions)
    again = await _get_test(item.id, user=s["student"], session=db)  # built once
    assert [q.id for q in again.questions] == [q.id for q in out.questions]


async def test_bank_first_then_generation_with_misconception_distractors(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _mcq(db, s, B.REMEMBER, "current")  # a checked bank MCQ on the slot's level and concept: used as is
    calls = []

    async def fake(payload):
        calls.append(payload)
        return {"questions": [
            {"slot_index": i, "text": f"gen {i}", "options": ["ok", "m1", "m2", "m3"], "answer": "ok"}
            for i in range(len(payload["slots"]))] + [
            {"slot_index": 0, "text": "bad", "options": ["a", "b"], "answer": "a"}]}  # malformed -> ignored
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "generate_mcq", fake)

    item = await _open(db, s)
    out = await _get_test(item.id, user=s["student"], session=db)
    assert len(out.questions) == 10
    assert sum(q.text.startswith("gen") for q in out.questions) == 9  # only the slots the bank couldn't fill
    assert any(q.text.startswith("REMEMBER current") for q in out.questions)
    [payload] = calls
    assert len(payload["slots"]) == 9 and all(sl["question_type"] == "mcq" for sl in payload["slots"])
    assert any(a.startswith("REMEMBER current") for a in payload["avoid"])  # never the same question twice
    current = next(sl for sl in payload["slots"] if sl["concept"] == "current")
    assert set(current["misconceptions"]) == {"current misconception 1", "current misconception 2"}
    assert payload["sections"][0]["text"].startswith("V = IR")
    rows = await service.questions_of(db, uuid.UUID(item.id))
    assert all(r.options[r.correct_index] == "ok" for r in rows if r.text.startswith("gen"))


async def test_an_untagged_bank_question_matches_through_its_topic(schema, monkeypatch):
    """Nool's banks tag topics, not concepts: a question on the topic the voice test
    asked a weak concept under fills that concept's slot without generation."""
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    q = await _mcq(db, s, B.REMEMBER, "current")
    q.concepts, q.topic_id = [], s["test"].topic_id
    await db.flush()
    calls = []

    async def fake(payload):
        calls.append(payload)
        return {"questions": []}
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "generate_mcq", fake)

    item = await _open(db, s)
    out = await _get_test(item.id, user=s["student"], session=db)
    assert any(x.text.startswith("REMEMBER current") for x in out.questions)
    assert len(calls[0]["slots"]) == 9  # the topic-matched slot never went to generation


async def test_a_bank_question_keeps_its_own_concept_tag(schema, monkeypatch):
    """C-006: a bank MCQ tagged "balancing" picked by topic for the "current" slot was
    labelled "current", so its gap_concept pointed the follow-ups at the wrong idea."""
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    q = await _mcq(db, s, B.REMEMBER, "balancing")
    q.topic_id = s["test"].topic_id
    await db.flush()

    async def fake(payload):
        return {"questions": []}
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "generate_mcq", fake)
    item = await _open(db, s)
    await _get_test(item.id, user=s["student"], session=db)
    [row] = [r for r in await service.questions_of(db, uuid.UUID(item.id)) if r.source_question_id == q.id]
    assert row.concept == "balancing" and row.gap_concept is None


async def test_generation_down_falls_back_to_any_bank_mcq_of_the_chapter(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    for n in range(10):
        await _mcq(db, s, B.CREATE, "other", n)  # no level/concept match at all

    async def down(payload):
        raise GenerationServiceUnavailableError("down")
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "generate_mcq", down)

    item = await _open(db, s)
    out = await _get_test(item.id, user=s["student"], session=db)
    assert len(out.questions) == 10 and len({q.text for q in out.questions}) == 10


# ------------------------------------------------------- marking, growth
async def _attempt(db, s, *, wrong_levels=()):
    item = await _open(db, s)
    await _get_test(item.id, user=s["student"], session=db)
    rows = await service.questions_of(db, uuid.UUID(item.id))
    answers, seen = [], set()
    for r in rows:
        # one of the two questions of each wrong level is answered wrong
        wrong = r.bloom_level in wrong_levels and r.bloom_level not in seen
        if wrong:
            seen.add(r.bloom_level)
        answers.append({"questionId": str(r.id), "optionIndex": (r.correct_index + 1) % 4 if wrong else r.correct_index})
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=answers), user=s["student"], session=db)
    return item, rows, out


def test_guess_correction():
    assert [corrected(p) for p in (0, 25, 50, 75, 100)] == [0, 0, 33, 67, 100]


def test_no_growth_for_a_level_asked_once():
    """V-008: Remember 28% -> 0% (-28) from one wrong re-check answer; under 2 asked = Not assessed."""
    entries = growth_entries("bloom", {"REMEMBER": 0, "UNDERSTAND": 100}, {"REMEMBER": 28, "UNDERSTAND": 0},
                             {"REMEMBER": 1, "UNDERSTAND": 2})
    assert [e["key"] for e in entries] == ["UNDERSTAND"]


async def test_attempt_marks_scores_growth_and_awards_xp_once(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    item, rows, out = await _attempt(db, s, wrong_levels=(B.UNDERSTAND,))
    assert out.score_percent == 90
    assert out.per_bloom == {"REMEMBER": 100, "UNDERSTAND": 50, "APPLY": 100, "ANALYZE": 100, "EVALUATE": 100}
    bloom = {g.key: g for g in out.growth if g.kind == "bloom"}
    assert (bloom["REMEMBER"].before, bloom["REMEMBER"].after_corrected, bloom["REMEMBER"].delta) == (20, 100, 80)
    assert (bloom["UNDERSTAND"].before, bloom["UNDERSTAND"].after_corrected, bloom["UNDERSTAND"].delta) == (50, 33, -17)
    concept = {g.key: g for g in out.growth if g.kind == "concept"}
    assert concept["current"].before == 35 and concept["resistance"].before == 35 and concept["power"].before == 90
    assert concept["power"].after_corrected == 100 and concept["power"].delta == 10
    assert {(a.question_id, a.option_index) for a in out.correct_answers} == {(str(r.id), r.correct_index) for r in rows}
    wire = out.model_dump(by_alias=True)
    assert set(wire) == {"scorePercent", "perBloom", "growth", "correctAnswers", "masteryBefore", "masteryAfter",
                         "xpAwarded", "xpBreakdown", "bestCombo", "gapsFixed", "classPercentile", "rank",
                         "missedQuestionIds"}
    # the 3rd question is wrong: combo bonus on questions 6..10, the boss (last) right
    assert wire["xpBreakdown"] == {"correct": 90, "comboBonus": 25, "bossBonus": 10, "speedBonus": 0,
                                   "lifelineCost": 0, "total": 125}
    assert out.xp_awarded == 125 and out.best_combo == 7 and out.mastery_after is not None
    assert set(wire["growth"][0]) == {"key", "kind", "before", "afterCorrected", "delta"}

    xp = (await db.execute(select(StudentGamification.xp).where(StudentGamification.student_id == s["student"].id))).scalar_one()
    assert xp == 125
    with pytest.raises(ConflictError):
        await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=[]), user=s["student"], session=db)
    assert (await _open(db, s)).status == "completed"


async def test_no_growth_for_a_level_the_voice_result_did_not_assess(schema, monkeypatch):
    """T-004: Results said Evaluate "Not assessed", Class growth showed Evaluate "4% before" (the mastery fill-in)."""
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    mine = select(StudentTestResult.id).where(StudentTestResult.student_id == s["student"].id).scalar_subquery()
    await db.execute(delete(StudentTestResultBloomScore).where(
        StudentTestResultBloomScore.student_test_result_id == mine, StudentTestResultBloomScore.bloom_level == B.EVALUATE))
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    _, _, out = await _attempt(db, s)
    assert "EVALUATE" in out.per_bloom  # asked on the re-check ...
    ev = next(g for g in out.growth if g.key == "EVALUATE" and g.kind == "bloom")  # S-012: shown, not hidden ...
    assert (ev.before, ev.delta) == (None, None)  # ... as 'Not assessed': no 'before' to grow from
    cls = await routes.get_test_growth(str(s["test"].id), user=s["teacher"], session=db)
    assert "EVALUATE" not in {b.level for b in cls.per_bloom}  # class view still skips it


async def test_attempt_before_opening_and_on_someone_elses_test(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    item = await _open(db, s)
    with pytest.raises(ConflictError):
        await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=[]), user=s["student"], session=db)
    other = _user(Role.STUDENT, s["school"].id)
    db.add(other)
    await db.flush()
    with pytest.raises(NotFoundError):
        await _get_test(item.id, user=other, session=db)


async def test_teacher_sees_class_growth_and_others_do_not(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    await _attempt(db, s, wrong_levels=(B.UNDERSTAND,))
    out = await routes.get_test_growth(str(s["test"].id), user=s["teacher"], session=db)
    [student] = out.students
    assert (student.name, student.before, student.after, student.delta) == ("Asha", 46, corrected(90), corrected(90) - 46)
    assert student.flagged is False  # 87 after the re-check: mastered
    per_bloom = {b.level: b for b in out.per_bloom}
    assert (per_bloom["UNDERSTAND"].before, per_bloom["UNDERSTAND"].after, per_bloom["UNDERSTAND"].delta) == (50, 33, -17)
    assert {c.concept for c in out.concepts} == {"current", "resistance", "power"}
    assert set(out.model_dump(by_alias=True)) == {"testId", "students", "perBloom", "concepts"}

    stranger = _user(Role.TEACHER, s["school"].id)
    db.add(stranger)
    await db.flush()
    with pytest.raises((ForbiddenError, NotFoundError)):
        await routes.get_test_growth(str(s["test"].id), user=stranger, session=db)
    other_school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="X", contact_email="a@b.com")
    db.add(other_school)
    await db.flush()
    outsider = _user(Role.TEACHER, other_school.id)
    db.add(outsider)
    await db.flush()
    with pytest.raises((ForbiddenError, NotFoundError)):
        await routes.get_test_growth(str(s["test"].id), user=outsider, session=db)


# ------------------------------------------------------------------ game
def _ans(ok, ms=None):
    return SimpleNamespace(correct=ok, elapsed_ms=ms)


def test_xp_breakdown_combo_boss_speed_and_lifelines():
    qs = [SimpleNamespace(id=i, is_boss=i == 5) for i in range(6)]
    answers = {0: _ans(True, 1000), 1: _ans(True, 20_000), 2: _ans(True), 3: _ans(False, 1000), 4: _ans(True, 15_000),
               5: _ans(True, 9000)}
    xp, streaks = service.xp_breakdown(qs, answers, speed_mode=False, lifelines=[])
    assert streaks == [1, 2, 3, 0, 1, 2]
    assert xp == {"correct": 50, "combo_bonus": 5, "boss_bonus": 10, "speed_bonus": 0, "lifeline_cost": 0, "total": 65}
    xp, _ = service.xp_breakdown(qs, answers, speed_mode=True, lifelines=["fifty_fifty", "ask_milo"])
    assert xp["speed_bonus"] == 9 and xp["lifeline_cost"] == 10 and xp["total"] == 64  # 1000, 15000, 9000 ms
    del answers[1]  # unanswered breaks the run
    _, streaks = service.xp_breakdown(qs, answers, speed_mode=False, lifelines=[])
    assert streaks == [1, 0, 1, 0, 1, 2]
    xp, _ = service.xp_breakdown(qs, {3: _ans(False)}, speed_mode=False, lifelines=["ask_milo"])
    assert xp["total"] == 0  # never below 0


def test_hint_never_names_the_answer():
    assert service.hint_of("current", "1 A", "It is 1 a, obviously") == "Think about current."
    assert service.hint_of("current", "1 A", "") == "Think about current."
    assert service.hint_of("current", "1 A", "What flows per second?") == "What flows per second?"
    assert service.explanation_of(["x", "y"], 1) == "The answer is y."


def test_the_boss_slot_is_analyze_at_least():
    assert service.plan_slots([B.REMEMBER], {"current": 30})[-1] == (B.ANALYZE, "current")
    assert service.plan_slots(LEVELS, {"current": 30})[-1][0] == B.EVALUATE


async def _ready(db, monkeypatch):
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    item = await _open(db, s)
    await _get_test(item.id, user=s["student"], session=db)
    return s, item, await service.questions_of(db, uuid.UUID(item.id))


def _answer(db, s, item, q, option, ms=None):
    return routes.answer_my_mcq_question(item.id, McqAnswerIn(question_id=str(q.id), option_index=option, elapsed_ms=ms),
                                         user=s["student"], session=db)


async def test_answer_keeps_only_the_first_answer_and_returns_the_marking(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)
    q = rows[0]
    out = await _answer(db, s, item, q, (q.correct_index + 1) % 4)
    assert out.model_dump(by_alias=True) == {"correct": False, "correctIndex": q.correct_index,
                                             "explanation": "The answer is right.", "combo": 0, "xpSoFar": 0}
    again = await _answer(db, s, item, q, q.correct_index)  # a second answer changes nothing
    assert again == out
    out = await _answer(db, s, item, rows[1], rows[1].correct_index)
    assert (out.correct, out.combo, out.xp_so_far) == (True, 1, 10)
    got = await _get_test(item.id, user=s["student"], session=db)
    assert [(a.question_id, a.correct) for a in got.answered] == [(str(rows[0].id), False), (str(rows[1].id), True)]
    assert got.answered[0].option_index == (q.correct_index + 1) % 4 and got.answered[0].correct_index == q.correct_index
    with pytest.raises(NotFoundError):
        await routes.answer_my_mcq_question(item.id, McqAnswerIn(question_id=str(uuid.uuid4()), option_index=0),
                                            user=s["student"], session=db)


async def test_speed_mode_only_before_the_first_answer_and_its_bonus(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)
    assert (await routes.set_my_mcq_speed_mode(item.id, SpeedModeIn(on=True), user=s["student"], session=db)).speed_mode
    for q in rows:
        await _answer(db, s, item, q, q.correct_index, 1000)
    with pytest.raises(ConflictError):
        await routes.set_my_mcq_speed_mode(item.id, SpeedModeIn(on=False), user=s["student"], session=db)
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(), user=s["student"], session=db)
    assert out.xp_breakdown.model_dump() == {"correct": 100, "combo_bonus": 40, "boss_bonus": 10, "speed_bonus": 30,
                                             "lifeline_cost": 0, "total": 180}
    assert out.xp_awarded == 180 and out.best_combo == 10 and out.score_percent == 100 and out.missed_question_ids == []
    with pytest.raises(ConflictError):  # completed
        await _answer(db, s, item, rows[0], 0)


async def test_lifelines_once_each_never_reveal_and_cost_xp(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)

    def use(kind, q):
        return routes.use_my_mcq_lifeline(item.id, LifelineIn(kind=kind, question_id=str(q.id)), user=s["student"],
                                          session=db)
    await _answer(db, s, item, rows[0], rows[0].correct_index)
    with pytest.raises(ConflictError):  # already answered
        await use("ask_milo", rows[0])
    half = await use("fifty_fifty", rows[1])
    assert len(half.hide_option_indexes) == 2 and rows[1].correct_index not in half.hide_option_indexes
    assert half.model_dump(by_alias=True, exclude_none=True) == {"hideOptionIndexes": half.hide_option_indexes}
    with pytest.raises(ConflictError):
        await use("fifty_fifty", rows[2])
    milo = await use("ask_milo", rows[2])
    assert milo.hint and "right" not in milo.hint.lower()
    with pytest.raises(ConflictError):
        await use("ask_milo", rows[3])
    got = await _get_test(item.id, user=s["student"], session=db)
    assert got.lifelines_left == []
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(), user=s["student"], session=db)
    assert out.xp_breakdown.lifeline_cost == 10 and out.xp_breakdown.total == 0  # only q1 answered: 10 - 10
    with pytest.raises(ConflictError):  # completed
        await use("ask_milo", rows[4])


async def test_finalise_from_recorded_answers_fills_the_rest_from_a_legacy_body(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)
    await _answer(db, s, item, rows[0], (rows[0].correct_index + 1) % 4)  # first answer: wrong
    await _answer(db, s, item, rows[1], rows[1].correct_index)
    body = [{"questionId": str(q.id), "optionIndex": q.correct_index} for q in rows]  # claims q1 right
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=body), user=s["student"], session=db)
    assert out.score_percent == 90 and out.missed_question_ids == [str(rows[0].id)]
    assert len(await service.answers_of(db, await db.get(McqTest, uuid.UUID(item.id)))) == 10
    gaps = [q for q in rows if q.gap_concept]
    assert out.gaps_fixed.total == len(gaps) == 9 and out.gaps_fixed.fixed == 8
    assert set(out.gaps_fixed.concepts) == {"current", "resistance"}
    assert out.class_percentile is None  # no classmates have done it


_rolls = itertools.count(2)


async def _classmate(db, s, *, score=None, points=None):
    other = _user(Role.STUDENT, s["school"].id)
    db.add(other)
    await db.flush()
    db.add(StudentProfile(user_id=other.id, class_id=s["class"].id, roll_number=next(_rolls)))
    if score is not None:
        now = datetime.now(timezone.utc)
        db.add(McqTest(student_id=other.id, voice_test_id=s["test"].id, unlocks_at=now, unlocked_at=now,
                       completed_at=now, score_percent=score))
    if points is not None:
        db.add(StudentPoints(student_id=other.id, points=points))
    await db.flush()


async def test_class_percentile_needs_three_classmates(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)
    for score in (50, 80, 100):
        await _classmate(db, s, score=score)
    body = [{"questionId": str(q.id), "optionIndex": q.correct_index if i else (q.correct_index + 1) % 4}
            for i, q in enumerate(rows)]
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=body), user=s["student"], session=db)
    assert out.score_percent == 90 and out.class_percentile == 67  # beat 2 of 3


async def test_class_percentile_is_null_under_three_classmates(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)
    for score in (50, 80):
        await _classmate(db, s, score=score)
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(), user=s["student"], session=db)
    assert out.class_percentile is None


async def test_rank_before_and_after_the_attempt_xp(schema, monkeypatch):
    db = schema
    s, item, rows = await _ready(db, monkeypatch)
    await _classmate(db, s, points=100)
    await _classmate(db, s, points=150)
    body = [{"questionId": str(q.id), "optionIndex": q.correct_index} for q in rows]
    out = await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=body), user=s["student"], session=db)
    assert out.xp_awarded == 150  # 100 + combo 40 + boss 10
    assert out.rank.model_dump() == {"before": 3, "after": 1}  # 0 -> 150 points, a tie shares rank 1
    points = (await db.execute(select(StudentPoints.points).where(StudentPoints.student_id == s["student"].id))).scalar_one()
    assert points == 150


async def test_generated_mcqs_carry_explanation_and_a_safe_hint_and_a_hard_boss(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    calls = []

    async def fake(payload):
        calls.append(payload)
        return {"questions": [{"slot_index": i, "text": f"gen {i}", "options": ["ok", "m1", "m2", "m3"], "answer": "ok",
                               "explanation": f"why {i}", "hint": "It is ok" if i == 0 else f"nudge {i}"}
                              for i in range(len(payload["slots"]))]}
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "generate_mcq", fake)
    item = await _open(db, s)
    await _get_test(item.id, user=s["student"], session=db)
    rows = await service.questions_of(db, uuid.UUID(item.id))
    assert [sl["difficulty"] for sl in calls[0]["slots"]] == ["MEDIUM"] * 9 + ["HARD"]
    assert rows[0].hint == f"Think about {rows[0].concept}." and rows[1].hint == "nudge 1"
    assert [r.explanation for r in rows] == [f"why {i}" for i in range(10)]
    assert rows[-1].is_boss and rows[-1].bloom_level in (B.ANALYZE, B.EVALUATE)


async def test_shuffled_options_keep_the_explanations_letters_and_lose_their_labels(schema, monkeypatch):
    """C-006: the options are shuffled after generation, but "Option (A) is correct" was kept,
    naming a wrong option; a concept with no misconceptions borrowed the other concepts'."""
    from sqlalchemy import select

    from src.domain.models import VoiceTestQuestion
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    for q in (await db.execute(select(VoiceTestQuestion))).scalars():
        if "power" in q.concepts:
            q.misconceptions = []
    await db.flush()
    calls = []

    async def fake(payload):
        calls.append(payload)
        return {"questions": [{"slot_index": i, "text": f"gen {i}", "answer": "(A) ok",
                               "options": ["(A) ok", "(B) m1", "(C) m2", "(D) m3"],
                               "explanation": "Option (A) is correct; options B and (c) are not."}
                              for i in range(len(payload["slots"]))]}
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "generate_mcq", fake)
    item = await _open(db, s)
    await _get_test(item.id, user=s["student"], session=db)
    rows = await service.questions_of(db, uuid.UUID(item.id))
    assert len(rows) == 10 and {r.options[r.correct_index] for r in rows} == {"ok"}
    for r in rows:
        assert sorted(r.options) == ["m1", "m2", "m3", "ok"]
        key, b, c = "ABCD"[r.correct_index], "ABCD"[r.options.index("m1")], "abcd"[r.options.index("m2")]
        assert r.explanation == f"Option ({key}) is correct; options {b} and ({c}) are not."
    assert next(sl for sl in calls[0]["slots"] if sl["concept"] == "power")["misconceptions"] == []


# ------------------------------------------------------------- migration
def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_mcq_tests.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w3_05_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "e2b7c9a4d1f3"  # chained after W3-04 at merge

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                step()
        return work

    def tables(sync_session):
        insp = inspect(sync_session.connection())
        return {t.name: ({c["name"] for c in insp.get_columns(t.name)} if insp.has_table(t.name) else None)
                for t in NEW_TABLES}

    if (await db_session.run_sync(tables))[McqTest.__tablename__] is None:
        await _ddl(db_session, run(module.upgrade))  # rolled back with the test's transaction
    found = await db_session.run_sync(tables)
    for table in NEW_TABLES:
        assert found[table.name] == {c.name for c in table.columns}
    # a later migration's table hangs off mcq_test_questions; drop it first (rolled back too)
    await _ddl(db_session, lambda conn: McqQuestionAnswer.__table__.drop(conn, checkfirst=True))
    await _ddl(db_session, run(module.downgrade))
    assert set((await db_session.run_sync(tables)).values()) == {None}


async def test_bank_questions_get_a_written_why_and_hint_and_keep_fallbacks_when_generation_is_down(monkeypatch):
    from src.services import generation_client
    from src.services import mcq_test as svc

    def draft():
        return {0: svc._Draft(BloomLevel.REMEMBER, "lime water", "Which gas turns lime water milky?",
                              ["Oxygen", "Carbon dioxide", "Hydrogen", "Nitrogen"], 1),
                1: svc._Draft(BloomLevel.APPLY, "alloys", "Brass is an alloy of?", ["a", "b", "c", "d"], 0,
                              explanation="Already written.", hint="Already a hint.")}

    sent = []

    async def explain(payload):
        sent.append(payload)
        return {"results": [{"question_number": 1, "explanation": "CO2 makes calcium carbonate.", "hint": None}]}

    monkeypatch.setattr(generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(generation_client, "explain_mcq", explain)
    drafts = draft()
    await svc._explain_bank(drafts)
    assert [i["question_number"] for i in sent[0]["items"]] == [1]  # only the one missing them
    assert drafts[0].explanation == "CO2 makes calcium carbonate." and drafts[0].hint == ""
    assert drafts[1].explanation == "Already written."

    async def down(payload):
        raise generation_client.GenerationServiceUnavailableError("down")

    monkeypatch.setattr(generation_client, "explain_mcq", down)
    drafts = draft()
    await svc._explain_bank(drafts)  # no raise; fallbacks apply when rows are built
    assert drafts[0].explanation == ""
    assert svc.explanation_of(drafts[0].options, 1, drafts[0].explanation) == "The answer is Carbon dioxide."


async def test_a_student_still_under_60_after_the_recheck_is_flagged(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    await _attempt(db, s, wrong_levels=tuple(B)[:4])  # 4 of 10 wrong: 60% -> 47 after guess correction
    [student] = (await routes.get_test_growth(str(s["test"].id), user=s["teacher"], session=db)).students
    assert student.after < 60 and student.flagged is True
