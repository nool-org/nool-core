"""Homework answers (contract C6), weak-concept targeting and the boss
question: the migration, homework_autogen's targeting from the student's own
graded voice test, POST /me/homework/{id}/questions/{qid}/answer (XP once,
double for the boss) and isBoss on GET /me/homework/{id}/questions.

Real rows against the dev Postgres via db_session. Until the dev DB has this
item's migration, the `schema` fixture adds homework_questions.is_boss and
creates homework_answers (without foreign keys) inside the test's own
transaction, so it's all rolled back - same pattern as
test_voice_test_questions.py.
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import undefer
from sqlalchemy.schema import CreateTable

from shared.errors import NotFoundError
from src.api.routes import student_homework
from src.api.schemas.homework import HomeworkAnswerIn
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
    HomeworkQuestion,
    HomeworkStatus,
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
    StudentTestResultTopicScore,
    Subject,
    Topic,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestQuestion,
)
from src.domain.models.homework import HomeworkAnswer
from src.domain.models.question_paper import PaperDifficultyLevel
from src.services import generation_client, homework_content_resolver
from src.services.homework_autogen import auto_generate_homework

B = BloomLevel
REVISION = "e2b7c9a4d1f3"


async def _ddl(db, work, attempts=5):
    """Runs `work` (sync, gets the connection) in a savepoint, retried when
    it deadlocks or waits too long on a lock held by another session."""
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
            if code not in ("40P01", "55P03") or attempt == attempts - 1:  # deadlock / lock timeout
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


def _create_new_schema(conn):
    conn.execute(text("ALTER TABLE homework_questions ADD COLUMN IF NOT EXISTS is_boss boolean NOT NULL DEFAULT false"))
    if not inspect(conn).has_table(HomeworkAnswer.__table__.name):
        conn.execute(CreateTable(HomeworkAnswer.__table__, include_foreign_key_constraints=[]))


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_new_schema)
    return db_session


def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_homework_answers_boss.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w3_04_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "c7badbe03a03"  # chained after W3-03 at merge

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                step()
        return work

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        return ({c["name"] for c in insp.get_columns("homework_questions")},
                {c["name"] for c in insp.get_columns("homework_answers")} if insp.has_table("homework_answers") else None)

    if (await db_session.run_sync(columns))[1] is None:  # not applied to this DB yet
        await _ddl(db_session, run(module.upgrade))  # rolled back with the test's transaction
    hq, answers = await db_session.run_sync(columns)
    assert "is_boss" in hq
    assert answers == {c.name for c in HomeworkAnswer.__table__.columns}

    await _ddl(db_session, run(module.downgrade))
    hq, answers = await db_session.run_sync(columns)
    assert "is_boss" not in hq and answers is None


# ------------------------------------------------------------------ seed
async def _seed(db) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    db.add_all([section, chapter])
    await db.flush()
    strong, weak = Topic(chapter_id=chapter.id, name="Ohm's law"), Topic(chapter_id=chapter.id, name="Resistors")
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    student = User(firebase_uid=f"s-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="S",
                   role=Role.STUDENT, school_id=school.id, status=UserStatus.ACTIVE)
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=subject.id, grade=10, board="CBSE")
    db.add_all([strong, weak, teacher, student, dataset,
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db.flush()
    db.add_all([SchoolDataset(school_id=school.id, dataset_id=dataset.id, enabled=True),
                StudentProfile(user_id=student.id, class_id=section.id, roll_number=uuid.uuid4().int % 100000)])
    test = VoiceTest(class_id=section.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=strong.id,
                     duration_minutes=10, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS)
    db.add(test)
    await db.flush()
    return {"test": test, "student": student, "teacher": teacher, "dataset": dataset, "chapter": chapter,
            "strong": strong, "weak": weak, "class": section}


def _bank_question(s, level, topic, concept, label) -> DatasetQuestion:
    return DatasetQuestion(dataset_id=s["dataset"].id, chapter_id=s["chapter"].id, topic_id=topic.id,
                           bloom_level=level, difficulty=PaperDifficultyLevel.MEDIUM,
                           question_type=QuestionType.SHORT_ANSWER, text=label, answer="a",
                           created_by=s["teacher"].id, external_uid=f"uid-{uuid.uuid4()}", concepts=[concept])


async def _graded_voice_test(db, s, scores: list[tuple[int, str | None]]):
    """The test's 3 questions (strong topic, then 2 on the weak one), the
    student's result with per-topic scores, and the graded session record."""
    specs = [(B.REMEMBER, s["strong"], "ohm's law"), (B.UNDERSTAND, s["weak"], "series resistance"),
             (B.APPLY, s["weak"], "Power")]
    db.add_all([VoiceTestQuestion(voice_test_id=s["test"].id, order=i, bloom_level=level, topic_id=topic.id,
                                  concepts=[concept], original_text="q", reworded_text="q", spoken_text="q",
                                  expected_points=[], misconceptions=[], hints=[])
                for i, (level, topic, concept) in enumerate(specs, start=1)])
    result = StudentTestResult(test_id=s["test"].id, student_id=s["student"].id, mastery_percent=50)
    ai_session = AiAssessorSession(student_id=s["student"].id, test_id=s["test"].id, context_label="t",
                                   duration_seconds=600)
    db.add_all([result, ai_session])
    await db.flush()
    db.add_all([
        StudentTestResultTopicScore(student_test_result_id=result.id, topic_id=s["strong"].id, percent=90, answered=1),
        StudentTestResultTopicScore(student_test_result_id=result.id, topic_id=s["weak"].id, percent=30, answered=2),
        AiAssessorSessionRecord(session_id=ai_session.id, needs_review=False, report={"qa_transcripts": [
            {"question_number": i, "score_percent": score, "misconception": misconception, "student_response": "x"}
            for i, (score, misconception) in enumerate(scores, start=1)]}),
    ])
    await db.flush()


async def _questions(db, homework_id) -> list[HomeworkQuestion]:
    return list((await db.execute(
        select(HomeworkQuestion).options(undefer(HomeworkQuestion.is_boss))
        .where(HomeworkQuestion.homework_id == homework_id).order_by(HomeworkQuestion.order))).scalars())


# -------------------------------------------------------------- targeting
async def test_auto_homework_targets_weak_concepts_and_marks_the_boss(schema, monkeypatch):
    db = schema
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)
    s = await _seed(db)
    bank = []
    for level in (B.REMEMBER, B.UNDERSTAND, B.APPLY):
        bank.append(_bank_question(s, level, s["strong"], "ohm's law", f"strong {level.value}"))
        bank.append(_bank_question(s, level, s["weak"], "series resistance", f"series {level.value}"))
        bank.append(_bank_question(s, level, s["weak"], "power", f"power {level.value}"))
    db.add_all(bank)
    # q2: 60 but a misconception (still weak), q3: 20 = lowest -> boss on "Power"
    await _graded_voice_test(db, s, [(90, None), (60, "thinks resistance adds in parallel"), (20, None)])

    await auto_generate_homework(db, test=s["test"], student_id=s["student"].id, mastery_percent=50,
                                 bloom_levels=[B.REMEMBER, B.UNDERSTAND, B.APPLY])
    await db.flush()

    hw = (await db.execute(select(Homework).where(Homework.test_id == s["test"].id))).scalar_one()
    questions = await _questions(db, hw.id)
    assert len(questions) == 5
    assert sorted(q.bloom_level.value for q in questions) == sorted(["REMEMBER", "REMEMBER", "UNDERSTAND",
                                                                     "UNDERSTAND", "APPLY"])  # same spread
    assert not any(q.text.startswith("strong") for q in questions)  # nothing from the topic they're good at
    bosses = [q for q in questions if q.is_boss]
    assert [b.text for b in bosses] == ["power APPLY"]


async def test_auto_homework_without_a_graded_session_has_no_boss(schema, monkeypatch):
    db = schema
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)
    s = await _seed(db)
    db.add_all([_bank_question(s, B.UNDERSTAND, s["strong"], "ohm's law", f"q{i}") for i in range(5)])
    await db.flush()

    await auto_generate_homework(db, test=s["test"], student_id=s["student"].id, mastery_percent=50,
                                 bloom_levels=[B.UNDERSTAND])
    await db.flush()

    hw = (await db.execute(select(Homework).where(Homework.test_id == s["test"].id))).scalar_one()
    questions = await _questions(db, hw.id)
    assert len(questions) == 5 and not any(q.is_boss for q in questions)


async def test_boss_slot_falls_back_to_the_legacy_generator_when_the_bank_has_nothing(schema, monkeypatch):
    db = schema
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)

    async def fake_legacy(session, *, count, bloom_distribution, **_kwargs):
        levels = [level for level, n in bloom_distribution.items() for _ in range(n)]
        from types import SimpleNamespace
        return [SimpleNamespace(bloom_level=level, text=f"legacy {i}", answer="a") for i, level in enumerate(levels)]

    monkeypatch.setattr(homework_content_resolver, "generate_questions", fake_legacy)
    s = await _seed(db)
    await _graded_voice_test(db, s, [(90, None), (70, None), (10, None)])

    await auto_generate_homework(db, test=s["test"], student_id=s["student"].id, mastery_percent=50,
                                 bloom_levels=[B.REMEMBER, B.APPLY])
    await db.flush()

    hw = (await db.execute(select(Homework).where(Homework.test_id == s["test"].id))).scalar_one()
    questions = await _questions(db, hw.id)
    assert len(questions) == 5
    assert [q.bloom_level for q in questions if q.is_boss] == [B.APPLY]


# ---------------------------------------------------------------- answers
async def _homework_with_questions(db, s) -> tuple[Homework, HomeworkQuestion, HomeworkQuestion]:
    hw = Homework(test_id=s["test"].id, class_id=s["class"].id, gap_topic="Resistors", gap_mastery_percent=40,
                  total_questions=2, difficulty=HomeworkDifficulty.MIXED,
                  target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS, status=HomeworkStatus.ASSIGNED,
                  assigned_count=1)
    db.add(hw)
    await db.flush()
    plain = HomeworkQuestion(homework_id=hw.id, order=1, bloom_level=B.UNDERSTAND, dataset_id=s["dataset"].id,
                             text="q1", answer="a1")
    boss = HomeworkQuestion(homework_id=hw.id, order=2, bloom_level=B.APPLY, dataset_id=s["dataset"].id,
                            text="q2", answer="a2", is_boss=True)
    db.add_all([plain, boss, StudentHomeworkProgress(homework_id=hw.id, student_id=s["student"].id)])
    await db.flush()
    return hw, plain, boss


async def _xp(db, student_id) -> int:
    state = (await db.execute(select(StudentGamification).where(StudentGamification.student_id == student_id)))
    row = state.scalar_one_or_none()
    return row.xp if row else 0


async def test_answer_is_saved_and_awards_xp_once_double_for_the_boss(schema):
    db = schema
    s = await _seed(db)
    hw, plain, boss = await _homework_with_questions(db, s)
    student = s["student"]

    async def answer(question, **body):
        return await student_homework.answer_question(str(hw.id), question.id, HomeworkAnswerIn(**body),
                                                      user=student, session=db)

    first = await answer(plain, answer_text="R adds up", self_rating="got_it")
    assert (first.saved, first.xp_awarded) == (True, 10)
    assert first.model_dump(by_alias=True) == {"saved": True, "xpAwarded": 10}
    again = await answer(plain, selfRating="confused")  # camelCase accepted too; no text keeps the old one
    assert again.xp_awarded == 0
    assert (await answer(boss, self_rating="got_it")).xp_awarded == 20
    assert await _xp(db, student.id) == 30

    rows = (await db.execute(select(HomeworkAnswer).where(HomeworkAnswer.student_id == student.id)
                             .order_by(HomeworkAnswer.xp_awarded))).scalars().all()
    assert [(r.homework_question_id, r.answer_text, r.self_rating, r.xp_awarded) for r in rows] == [
        (plain.id, "R adds up", "confused", 10), (boss.id, None, "got_it", 20)]
    progress = (await db.execute(select(StudentHomeworkProgress).where(
        StudentHomeworkProgress.homework_id == hw.id))).scalar_one()
    assert progress.status == StudentHomeworkStatus.IN_PROGRESS and not progress.qa_completed

    # confirm-completion still works once everything is answered
    await student_homework.confirm_completion(str(hw.id), user=student, session=db)
    await db.refresh(progress)
    assert progress.status == StudentHomeworkStatus.COMPLETED


async def test_answer_rejects_another_homeworks_question_and_unassigned_students(schema):
    db = schema
    s = await _seed(db)
    hw, plain, _ = await _homework_with_questions(db, s)
    other_hw, other_q, _ = await _homework_with_questions(db, s)
    body = HomeworkAnswerIn(self_rating="got_it")
    with pytest.raises(NotFoundError):
        await student_homework.answer_question(str(hw.id), other_q.id, body, user=s["student"], session=db)
    stranger = User(firebase_uid=f"x-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="X",
                    role=Role.STUDENT, school_id=s["student"].school_id, status=UserStatus.ACTIVE)
    db.add(stranger)
    await db.flush()
    with pytest.raises(NotFoundError):
        await student_homework.answer_question(str(hw.id), plain.id, body, user=stranger, session=db)


async def test_answer_body_validates_the_self_rating():
    with pytest.raises(ValueError):
        HomeworkAnswerIn(self_rating="meh")


async def test_questions_endpoint_flags_the_boss(schema):
    db = schema
    s = await _seed(db)
    hw, _, _ = await _homework_with_questions(db, s)
    items = (await student_homework.get_my_homework_questions(str(hw.id), user=s["student"], session=db)).items
    assert [q.model_dump(by_alias=True)["isBoss"] for q in items] == [False, True]
