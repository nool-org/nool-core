"""The questions every student of a Voice Test is asked: the migration,
the prep job (services/voice_test_prep.py) and its fallbacks, the teacher
review routes, the live session config, and scoring at finish
(per-topic scores, raw report storage, grading_error -> needs review).

Real rows against the dev Postgres via db_session. Until the dev DB has
this item's migration, the `schema` fixture creates the new tables inside
the test's own transaction, so they're rolled back with everything else.
"""

import importlib.util
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from src.api.routes import ai_assessor, voice_test
from src.api.schemas.ai_assessor import FinishSessionIn
from src.api.schemas.voice_test import UpdateVoiceTestQuestionIn
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionBloomLevel,
    AiAssessorSessionRecord,
    AssignmentTargetMode,
    Base,
    BloomLevel,
    Chapter,
    Dataset,
    DatasetQuestion,
    QuestionType,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    SchoolDataset,
    StudentTestResult,
    StudentTestResultBloomScore,
    StudentTestResultTopicScore,
    Subject,
    TeacherClassAssignment,
    TestStatus,
    Topic,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestQuestion,
    VoiceTestQuestionStatus,
)
from src.domain.models.question_paper import PaperDifficultyLevel
from src.services import voice_test_prep
from src.services.generation_client import GenerationServiceUnavailableError

B = BloomLevel
NEW_TABLES = [VoiceTestQuestion.__table__, StudentTestResultTopicScore.__table__, AiAssessorSessionRecord.__table__]
REVISION = "b3d9e1f04a7c"


@pytest_asyncio.fixture
async def schema(db_session):
    await db_session.run_sync(lambda s: Base.metadata.create_all(s.connection(), tables=NEW_TABLES, checkfirst=True))
    return db_session


# ------------------------------------------------------------------ seed
async def _teacher(db, school_id) -> User:
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                   role=Role.TEACHER, school_id=school_id, status=UserStatus.ACTIVE)
    db.add(teacher)
    await db.flush()
    return teacher


async def _seed(db, levels=(B.REMEMBER, B.UNDERSTAND, B.APPLY)) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    db.add_all([section, chapter])
    await db.flush()
    teacher = await _teacher(db, school.id)
    topic = Topic(chapter_id=chapter.id, name="Ohm's law")
    other_topic = Topic(chapter_id=chapter.id, name="Resistors in series")
    db.add_all([topic, other_topic,
                TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    dataset = Dataset(name="Important", question_count=0, description="", subject_id=subject.id, grade=10,
                      board="CBSE")
    db.add(dataset)
    await db.flush()
    db.add(SchoolDataset(school_id=school.id, dataset_id=dataset.id, enabled=True))
    test = VoiceTest(class_id=section.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=topic.id,
                     duration_minutes=10, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS)
    db.add(test)
    await db.flush()
    db.add_all([VoiceTestBloomLevel(test_id=test.id, bloom_level=level) for level in levels])
    await db.flush()
    return {"school": school, "teacher": teacher, "test": test, "topic": topic, "other_topic": other_topic,
            "dataset": dataset, "chapter": chapter, "class": section}


async def _question(db, s, level, text, *, topic=None, qtype=QuestionType.SHORT_ANSWER) -> DatasetQuestion:
    q = DatasetQuestion(dataset_id=s["dataset"].id, chapter_id=s["chapter"].id,
                        topic_id=(topic or s["topic"]).id, bloom_level=level, difficulty=PaperDifficultyLevel.MEDIUM,
                        question_type=qtype, text=text, answer=f"answer to {text}", created_by=s["teacher"].id,
                        external_uid=f"uid-{uuid.uuid4()}", concepts=["current"])
    db.add(q)
    await db.flush()
    return q


def _fake_kg(monkeypatch):
    async def fake(**kwargs):
        return {"textbook_context": "V = IR", "reference_questions": []}
    monkeypatch.setattr(voice_test_prep.kg_client, "generate_assessor_content", fake)


def _fake_generation(monkeypatch, calls=None):
    async def fake(payload):
        if calls is not None:
            calls.append(payload)
        return {"questions": [{
            "dataset_question_id": c["id"], "bloom_level": c["bloom_level"], "original_text": c["text"],
            "reworded_text": f"Simple: {c['text']}", "spoken_text": f"Spoken: {c['text']}",
            "expected_points": ["p1", "p2"], "misconceptions": ["m1", "m2"], "hints": ["h1", "h2"],
            "concepts": ["current"],
        } for c in payload["candidates"]]}
    monkeypatch.setattr(voice_test_prep.generation_client, "prep_voice_test", fake)


async def _questions(db, test) -> list[VoiceTestQuestion]:
    return list((await db.execute(select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test.id)
                                  .order_by(VoiceTestQuestion.order))).scalars().all())


# ------------------------------------------------------------- migration
def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_voice_test_questions.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w2_01_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_creates_what_the_models_map(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    current = (await db_session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    if current != REVISION:
        assert module.down_revision == current, "migration must sit on the dev DB's current head"
        await db_session.execute(text("SET LOCAL lock_timeout = '10s'"))

        def upgrade(sync_session):
            with Operations.context(MigrationContext.configure(sync_session.connection())):
                module.upgrade()
        await db_session.run_sync(upgrade)  # rolled back with the test's transaction

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        return {t.name: {c["name"] for c in insp.get_columns(t.name)} for t in [*NEW_TABLES, School.__table__]}
    found = await db_session.run_sync(columns)
    for table in NEW_TABLES:
        assert found[table.name] == {c.name for c in table.columns}
    assert "voice_pipeline" not in found["schools"]


# ------------------------------------------------------------------ prep
async def test_prep_picks_one_question_per_level_in_bloom_order_and_rewords_it(schema, monkeypatch):
    db = schema
    s = await _seed(db, levels=(B.APPLY, B.REMEMBER))
    remember = await _question(db, s, B.REMEMBER, "Define electric current.")
    await _question(db, s, B.REMEMBER, "Which of the following is the unit of current?", qtype=QuestionType.MCQ)
    apply_q = await _question(db, s, B.APPLY, "Find the current through a 4 ohm resistor at 12 V.",
                              topic=s["other_topic"])
    _fake_kg(monkeypatch)
    calls = []
    _fake_generation(monkeypatch, calls)

    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    rows = await _questions(db, s["test"])
    assert [(r.order, r.bloom_level, r.dataset_question_id) for r in rows] == [
        (1, B.REMEMBER, remember.id), (2, B.APPLY, apply_q.id)]  # MCQ avoided while others fit
    assert rows[0].reworded_text == "Simple: Define electric current."
    assert rows[0].spoken_text == "Spoken: Define electric current."
    assert rows[0].original_text == "Define electric current."
    assert (rows[0].expected_points, rows[0].misconceptions, rows[0].hints) == (["p1", "p2"], ["m1", "m2"], ["h1", "h2"])
    assert rows[1].topic_id == s["other_topic"].id
    assert all(r.status == VoiceTestQuestionStatus.DRAFT for r in rows)
    assert s["test"].textbook_context == "V = IR"
    assert calls[0]["levels"] == ["REMEMBER", "APPLY"] and calls[0]["board"] == "CBSE" and calls[0]["grade"] == 10
    assert calls[0]["candidates"][0]["answer"] == "answer to Define electric current."

    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])  # a retry never re-picks
    assert len(calls) == 1 and len(await _questions(db, s["test"])) == 2


async def test_prep_falls_back_to_dataset_text_when_generation_is_down(schema, monkeypatch, caplog):
    db = schema
    s = await _seed(db, levels=(B.REMEMBER,))
    await _question(db, s, B.REMEMBER, "Define resistance.")
    _fake_kg(monkeypatch)

    async def down(payload):
        raise GenerationServiceUnavailableError("down")
    monkeypatch.setattr(voice_test_prep.generation_client, "prep_voice_test", down)

    with caplog.at_level(logging.WARNING):
        await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    [row] = await _questions(db, s["test"])
    assert row.original_text == row.reworded_text == row.spoken_text == "Define resistance."
    assert row.expected_points == row.misconceptions == row.hints == []
    assert "voice_test_prep_generation_unavailable" in caplog.text


async def test_prep_skips_a_level_with_no_question_and_logs(schema, monkeypatch, caplog):
    db = schema
    s = await _seed(db, levels=(B.REMEMBER, B.EVALUATE))
    await _question(db, s, B.REMEMBER, "Define resistance.")
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    assert [r.bloom_level for r in await _questions(db, s["test"])] == [B.REMEMBER]
    assert "voice_test_prep_no_question_for_level" in caplog.text


async def test_prep_with_no_question_at_all_keeps_textbook_context_only(schema, monkeypatch, caplog):
    db = schema
    s = await _seed(db)
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    assert await _questions(db, s["test"]) == []
    assert s["test"].textbook_context == "V = IR"
    assert "voice_test_prep_no_questions" in caplog.text


async def test_create_test_schedules_prep_in_the_background(db_session):
    from src.api.schemas.roster import AssignmentTarget
    from src.api.schemas.voice_test import CreateTestIn

    s = await _seed(db_session)

    class Tasks:
        scheduled = []

        def add_task(self, func, *args):
            self.scheduled.append((func, args))

    tasks = Tasks()
    out = await voice_test.create_test(
        CreateTestIn(class_id=str(s["class"].id), subject_id=str(s["test"].subject_id), chapter_id=str(s["chapter"].id),
                     topic_id=str(s["topic"].id), bloom_levels=[B.REMEMBER], duration_minutes=10,
                     completion_window_hours=48, target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS)),
        background_tasks=tasks, user=s["teacher"], session=db_session)
    assert tasks.scheduled == [(voice_test_prep.prepare_in_background, (uuid.UUID(out.id), s["teacher"].id))]


# ---------------------------------------------------------------- review
async def _prepared(db, monkeypatch, levels=(B.REMEMBER, B.UNDERSTAND)):
    s = await _seed(db, levels=levels)
    for level in levels:
        await _question(db, s, level, f"{level.value} question one")
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)
    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])
    return s


async def test_teacher_lists_edits_approves_and_swaps_questions(schema, monkeypatch):
    db = schema
    s = await _prepared(db, monkeypatch)
    kw = dict(user=s["teacher"], _feature=s["teacher"], session=db)
    test_id = str(s["test"].id)

    listed = await voice_test.list_test_questions(test_id, **kw)
    assert [q.order for q in listed.items] == [1, 2]
    first = listed.items[0]
    assert first.model_dump(by_alias=True)["spokenText"].startswith("Spoken:")

    edited = await voice_test.update_test_question(
        test_id, first.id, UpdateVoiceTestQuestionIn(spoken_text=" Say it slowly ", status="approved"), **kw)
    assert edited.spoken_text == "Say it slowly" and edited.status == VoiceTestQuestionStatus.APPROVED

    with pytest.raises(ConflictError):  # no other REMEMBER question yet
        await voice_test.swap_test_question(test_id, first.id, **kw)
    other = await _question(db, s, B.REMEMBER, "Another remember question")
    swapped = await voice_test.swap_test_question(test_id, first.id, **kw)
    assert (swapped.id, swapped.order) == (first.id, 1)
    assert swapped.dataset_question_id == str(other.id)
    assert swapped.reworded_text == "Simple: Another remember question"
    assert swapped.status == VoiceTestQuestionStatus.DRAFT

    with pytest.raises(NotFoundError):
        await voice_test.update_test_question(test_id, str(uuid.uuid4()), UpdateVoiceTestQuestionIn(), **kw)
    with pytest.raises(NotFoundError):
        await voice_test.update_test_question(test_id, "not-a-uuid", UpdateVoiceTestQuestionIn(), **kw)


async def test_only_the_tests_teacher_may_review_and_not_after_students_took_it(schema, monkeypatch):
    db = schema
    s = await _prepared(db, monkeypatch)
    test_id = str(s["test"].id)
    [first, _] = await _questions(db, s["test"])

    stranger = await _teacher(db, s["school"].id)  # same school, doesn't teach this class
    with pytest.raises(ForbiddenError):
        await voice_test.list_test_questions(test_id, user=stranger, _feature=stranger, session=db)
    with pytest.raises(ForbiddenError):
        await voice_test.swap_test_question(test_id, str(first.id), user=stranger, _feature=stranger, session=db)
    other_school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Madurai", contact_email="c@d.com")
    db.add(other_school)
    await db.flush()
    outsider = await _teacher(db, other_school.id)
    with pytest.raises(NotFoundError):
        await voice_test.update_test_question(test_id, str(first.id), UpdateVoiceTestQuestionIn(status="approved"),
                                              user=outsider, _feature=outsider, session=db)

    s["test"].completed_count = 1
    with pytest.raises(ConflictError):
        await voice_test.update_test_question(test_id, str(first.id), UpdateVoiceTestQuestionIn(status="approved"),
                                              user=s["teacher"], _feature=s["teacher"], session=db)


# ------------------------------------------------------- session + finish
async def _student_session(db, s) -> tuple[User, AiAssessorSession]:
    student = User(firebase_uid=f"s-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="S",
                   role=Role.STUDENT, school_id=s["school"].id, status=UserStatus.ACTIVE)
    db.add(student)
    await db.flush()
    ai_session = AiAssessorSession(student_id=student.id, test_id=s["test"].id, context_label="x", duration_seconds=600)
    db.add(ai_session)
    await db.flush()
    await db.refresh(ai_session)  # created_at is a server default
    levels = [r.bloom_level for r in await _questions(db, s["test"])]
    db.add_all([AiAssessorSessionBloomLevel(session_id=ai_session.id, bloom_level=level) for level in levels])
    await db.flush()
    return student, ai_session


async def test_session_config_carries_the_tests_questions_in_order(schema, monkeypatch):
    db = schema
    s = await _prepared(db, monkeypatch)
    _student, ai_session = await _student_session(db, s)

    config, count = await ai_assessor._colearner_config(db, ai_session, [B.REMEMBER, B.UNDERSTAND])

    rows = await _questions(db, s["test"])
    assert count == 2 and config["num_questions"] == 2
    assert config["questions"] == [{
        "id": str(r.id), "order": r.order, "bloom_level": r.bloom_level.value, "spoken_text": r.spoken_text,
        "expected_points": r.expected_points, "misconceptions": r.misconceptions, "hints": r.hints,
    } for r in rows]
    assert config["textbook_context"] == "V = IR"
    assert "reference_questions" not in config


def _wire_finish(monkeypatch, db, payload):
    @asynccontextmanager
    async def same_session():
        yield db

    async def fake_finish(**kwargs):
        return payload
    monkeypatch.setattr(ai_assessor, "get_session", same_session)
    monkeypatch.setattr(ai_assessor, "finish_realtime_call", fake_finish)
    monkeypatch.setattr(ai_assessor, "schedule_auto_homework", lambda **kwargs: None)


def _qa(number, level, score, response="my answer"):
    return {"question_number": number, "blooms_attribute": level, "score_percent": score, "student_response": response}


async def test_finish_records_per_topic_scores_and_keeps_the_raw_payload(schema, monkeypatch):
    db = schema
    s = await _seed(db, levels=(B.REMEMBER, B.UNDERSTAND, B.APPLY))
    await _question(db, s, B.REMEMBER, "r")
    await _question(db, s, B.UNDERSTAND, "u")
    await _question(db, s, B.APPLY, "a", topic=s["other_topic"])
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)
    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])
    s["test"].status = TestStatus.SCHEDULED
    student, ai_session = await _student_session(db, s)
    payload = {
        "report": {"qa_transcripts": [_qa(1, "Remembering", 80), _qa(2, "Understanding", 40, response=""),
                                      _qa(3, "Applying", 90)], "session_summary": {}},
        "answers": [{"question_id": "x", "question_number": 1, "first_answer": "a", "hints_used": 1,
                     "explanation_requested": False}],
        "transcript": [{"role": "assistant", "text": "Hi", "question_number": None}],
        "answered": 3, "model_completed": True, "cost": {"total_usd": 0.01},
    }
    _wire_finish(monkeypatch, db, payload)

    out = await ai_assessor.finish_session(str(ai_session.id), FinishSessionIn(call_id="c1"), authorization="Bearer t",
                                           app_user=student, db=db)

    assert out.outcome == "completed" and out.summary.total_questions == 3
    result = (await db.execute(select(StudentTestResult).where(StudentTestResult.test_id == s["test"].id))).scalar_one()
    assert result.mastery_percent == 70
    topics = {t.topic_id: (t.percent, t.answered) for t in (await db.execute(select(StudentTestResultTopicScore).where(
        StudentTestResultTopicScore.student_test_result_id == result.id))).scalars()}
    assert topics == {s["topic"].id: (60, 1), s["other_topic"].id: (90, 1)}
    blooms = {b.bloom_level: b.percent for b in (await db.execute(select(StudentTestResultBloomScore).where(
        StudentTestResultBloomScore.student_test_result_id == result.id))).scalars()}
    assert blooms == {B.REMEMBER: 80, B.UNDERSTAND: 40, B.APPLY: 90}
    record = await db.get(AiAssessorSessionRecord, ai_session.id)
    assert record.transcript == payload["transcript"] and record.answers == payload["answers"]
    assert record.report == payload["report"] and record.needs_review is False


async def test_grading_error_records_no_score_and_flags_for_review(schema, monkeypatch, caplog):
    db = schema
    s = await _prepared(db, monkeypatch)
    s["test"].status = TestStatus.SCHEDULED
    student, ai_session = await _student_session(db, s)
    report = {"qa_transcripts": [], "session_summary": {"overall_mastery_percent": 0},
              "grading_error": "APITimeoutError: timed out"}
    _wire_finish(monkeypatch, db, {"report": report, "answers": [], "transcript": [], "answered": 2,
                                   "model_completed": True})

    with caplog.at_level(logging.WARNING):
        out = await ai_assessor.finish_session(str(ai_session.id), FinishSessionIn(call_id="c1"),
                                               authorization="Bearer t", app_user=student, db=db)

    assert out.outcome == "completed"
    assert (await db.execute(select(StudentTestResult).where(StudentTestResult.test_id == s["test"].id))).first() is None
    record = await db.get(AiAssessorSessionRecord, ai_session.id)
    assert record.needs_review is True and record.report["grading_error"]
    assert "ai_assessor_grading_failed_needs_review" in caplog.text
