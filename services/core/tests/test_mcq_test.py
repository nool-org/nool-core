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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from src.api.routes import mcq_test as routes
from src.api.schemas.mcq_test import McqAttemptIn
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
from src.services.growth import corrected

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


# ---------------------------------------------------------------- unlock
async def test_fresh_result_is_locked_until_48h_after_the_test(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=1)
    item = await _open(db, s)
    assert item.status == "locked" and item.question_count == 10
    assert item.unlocks_at > datetime.now(timezone.utc) + timedelta(hours=46)
    with pytest.raises(ConflictError):
        await routes.get_my_mcq_test(item.id, user=s["student"], session=db)
    with pytest.raises(ConflictError):
        await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=[]), user=s["student"], session=db)


async def test_unlocks_48h_after_the_voice_test(schema):
    s = await _seed(schema, taken_hours_ago=49)
    item = await _open(schema, s)
    assert item.status == "available" and item.unlocks_at is None
    assert item.title == "Electricity - MCQ check" and item.test_id == str(s["test"].id)


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
    out = await routes.get_my_mcq_test(item.id, user=s["student"], session=db)
    assert len(out.questions) == 10
    assert [q.bloom_level for q in out.questions] == [lvl.value for lvl in LEVELS for _ in range(2)]
    assert sum(q.concept == "power" for q in out.questions) == 1
    assert all(q.concept in ("current", "resistance") for q in out.questions if q.concept != "power")
    assert all(q.text.split()[1] == q.concept for q in out.questions)  # bank question on the slot's concept
    wire = out.model_dump_json(by_alias=True)
    assert "right" in wire and "correct" not in wire.lower() and "answer" not in wire.lower()
    again = await routes.get_my_mcq_test(item.id, user=s["student"], session=db)  # built once
    assert [q.id for q in again.questions] == [q.id for q in out.questions]


async def test_generation_designs_every_mcq_first_with_misconception_distractors(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _mcq(db, s, B.REMEMBER, "current")  # a bank MCQ exists, but generation comes first
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
    out = await routes.get_my_mcq_test(item.id, user=s["student"], session=db)
    assert len(out.questions) == 10 and sum(q.text.startswith("gen") for q in out.questions) == 10
    [payload] = calls
    assert len(payload["slots"]) == 10 and all(sl["question_type"] == "mcq" for sl in payload["slots"])
    current = next(sl for sl in payload["slots"] if sl["concept"] == "current")
    assert set(current["misconceptions"]) == {"current misconception 1", "current misconception 2"}
    assert payload["sections"][0]["text"].startswith("V = IR")
    rows = await service.questions_of(db, uuid.UUID(item.id))
    assert all(r.options[r.correct_index] == "ok" for r in rows if r.text.startswith("gen"))


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
    out = await routes.get_my_mcq_test(item.id, user=s["student"], session=db)
    assert len(out.questions) == 10 and len({q.text for q in out.questions}) == 10


# ------------------------------------------------------- marking, growth
async def _attempt(db, s, *, wrong_levels=()):
    item = await _open(db, s)
    await routes.get_my_mcq_test(item.id, user=s["student"], session=db)
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
    assert set(wire) == {"scorePercent", "perBloom", "growth", "correctAnswers"}
    assert set(wire["growth"][0]) == {"key", "kind", "before", "afterCorrected", "delta"}

    xp = (await db.execute(select(StudentGamification.xp).where(StudentGamification.student_id == s["student"].id))).scalar_one()
    assert xp == 9 * service.XP_PER_CORRECT
    with pytest.raises(ConflictError):
        await routes.submit_my_mcq_attempt(item.id, McqAttemptIn(answers=[]), user=s["student"], session=db)
    assert (await _open(db, s)).status == "completed"


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
        await routes.get_my_mcq_test(item.id, user=other, session=db)


async def test_teacher_sees_class_growth_and_others_do_not(schema, monkeypatch):
    db = schema
    s = await _seed(db, taken_hours_ago=49)
    await _full_bank(db, s)
    _no_generation(monkeypatch)
    await _attempt(db, s, wrong_levels=(B.UNDERSTAND,))
    out = await routes.get_test_growth(str(s["test"].id), user=s["teacher"], session=db)
    [student] = out.students
    assert (student.name, student.before, student.after, student.delta) == ("Asha", 46, corrected(90), corrected(90) - 46)
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
    await _ddl(db_session, run(module.downgrade))
    assert set((await db_session.run_sync(tables)).values()) == {None}
