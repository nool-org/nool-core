"""Wave-2 follow-ups: VoiceTest.prep_status (migration, set by the prep job,
gates opening a session), questionCount on POST /ai-assessor/sessions, the
prep's candidate filter, and a needs-review attempt leaving the student's
pending lists.

Until the dev DB has this item's migration, `schema` runs its upgrade()
inside the test's own transaction (rolled back with everything else).
"""

import importlib.util
import logging
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from shared.errors import ConflictError
from src.api.routes import ai_assessor, assigned_test, student_dashboard
from src.api.schemas.ai_assessor import OpenSessionIn
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    QuestionType,
    Role,
    TestStatus,
    User,
    UserStatus,
    VoiceTestTargetStudent,
)
from src.domain.models.voice_test import VoiceTestPrepStatus
from src.services import voice_test_prep
from tests.test_voice_test_questions import (
    B,
    _create_new_tables,
    _ddl,
    _fake_generation,
    _fake_kg,
    _question,
    _questions,
    _seed,
)

REVISION = "c7e2a9d4f1b3"


def _migration():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_voice_test_prep_status.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w3_09_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


def _run(fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def work(conn):
        with Operations.context(MigrationContext.configure(conn)):
            fn()
    return work


def _prep_columns(conn):
    return {c["name"] for c in inspect(conn).get_columns("voice_tests")}


async def _has_prep_status(db) -> bool:
    return "prep_status" in await db.run_sync(lambda s: _prep_columns(s.connection()))


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_new_tables)
    if not await _has_prep_status(db_session):
        await _ddl(db_session, _run(_migration().upgrade))
    return db_session


async def test_migration_adds_prep_status_and_downgrade_drops_it(db_session):
    module = _migration()
    assert module.down_revision == "b3d9e1f04a7c"
    if not await _has_prep_status(db_session):
        await _ddl(db_session, _run(module.upgrade))
    assert await _has_prep_status(db_session)
    default = (await db_session.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'voice_tests' AND column_name = 'prep_status'"))).scalar_one()
    assert "READY" in default  # existing tests are never blocked
    await _ddl(db_session, _run(module.downgrade))  # rolled back with the test's transaction
    assert not await _has_prep_status(db_session)


# ------------------------------------------------------------------ prep
async def test_prep_marks_ready_with_questions_and_failed_without(schema, monkeypatch):
    db = schema
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)
    s = await _seed(db, levels=(B.REMEMBER,))
    await _question(db, s, B.REMEMBER, "Define resistance.")
    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])
    assert s["test"].prep_status == VoiceTestPrepStatus.READY

    empty = await _seed(db)
    await voice_test_prep.prepare_test(db, empty["test"], empty["teacher"])
    assert empty["test"].prep_status == VoiceTestPrepStatus.FAILED


async def test_prep_never_picks_unspeakable_questions_and_logs_them(schema, monkeypatch, caplog):
    db = schema
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)
    s = await _seed(db, levels=(B.REMEMBER,))
    await _question(db, s, B.REMEMBER, "Which is the unit of current?", qtype=QuestionType.MCQ)
    await _question(db, s, B.REMEMBER, "Which is the unit of current? (a) volt (b) ampere (c) ohm (d) watt")
    await _question(db, s, B.REMEMBER, "State {tex}V = IR{/tex} in words.")
    await _question(db, s, B.REMEMBER, "Define the � unit of charge.")

    with caplog.at_level(logging.INFO):
        await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    assert await _questions(db, s["test"]) == []  # nothing speakable: improvise, not an MCQ
    assert s["test"].prep_status == VoiceTestPrepStatus.FAILED
    assert "voice_test_prep_skipped_candidates" in caplog.text

    good = await _question(db, s, B.REMEMBER, "Define electric charge.")
    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])
    assert [q.dataset_question_id for q in await _questions(db, s["test"])] == [good.id]
    assert s["test"].prep_status == VoiceTestPrepStatus.READY


async def test_prep_in_background_marks_failed_on_a_crash(monkeypatch):
    class Session:
        def __init__(self):
            self.test = type("T", (), {"prep_status": VoiceTestPrepStatus.PENDING})()
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return self.test

        async def rollback(self):
            pass

        async def commit(self):
            self.commits += 1

    session = Session()

    async def crash(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(voice_test_prep, "get_session", lambda: session)
    monkeypatch.setattr(voice_test_prep, "prepare_test", crash)

    await voice_test_prep.prepare_in_background("t", "u")

    assert session.test.prep_status == VoiceTestPrepStatus.FAILED and session.commits == 1


# --------------------------------------------------------------- session
async def _targeted_student(db, s) -> User:
    student = User(firebase_uid=f"s-{s['test'].id}", email=f"{s['test'].id}@example.com", display_name="S",
                   role=Role.STUDENT, school_id=s["school"].id, status=UserStatus.ACTIVE)
    db.add(student)
    await db.flush()
    db.add(VoiceTestTargetStudent(test_id=s["test"].id, student_id=student.id))
    s["test"].status = TestStatus.SCHEDULED
    await db.flush()
    return student


def _open(test) -> OpenSessionIn:
    return OpenSessionIn(test_id=str(test.id), context_label="x", bloom_levels=[B.REMEMBER], duration_seconds=600)


async def test_open_session_waits_for_prep_and_returns_the_question_count(schema, monkeypatch):
    db = schema
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)
    s = await _seed(db, levels=(B.REMEMBER, B.UNDERSTAND))
    student = await _targeted_student(db, s)

    s["test"].prep_status = VoiceTestPrepStatus.PENDING
    await db.flush()
    with pytest.raises(ConflictError, match="getting this test's questions ready"):
        await ai_assessor.open_session(_open(s["test"]), app_user=student, db=db)

    s["test"].prep_status = VoiceTestPrepStatus.FAILED  # no questions: open, improvise
    await db.flush()
    out = await ai_assessor.open_session(_open(s["test"]), app_user=student, db=db)
    assert out.model_dump(by_alias=True)["questionCount"] == 0

    await _question(db, s, B.REMEMBER, "Define resistance.")
    await _question(db, s, B.UNDERSTAND, "Explain why a wire heats up.")
    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])
    out = await ai_assessor.open_session(_open(s["test"]), app_user=student, db=db)
    assert out.question_count == 2


async def test_reopening_an_unfinished_test_carries_on_the_same_session(schema, monkeypatch):
    """V-001: a reload mid-test opens the session again - it must be the same one (colearner
    then resumes its answers and clock), not a second one from zero that mixes into the result."""
    db = schema
    s = await _seed(db, levels=(B.REMEMBER,))
    student = await _targeted_student(db, s)
    s["test"].prep_status = VoiceTestPrepStatus.FAILED
    await db.flush()

    first = await ai_assessor.open_session(_open(s["test"]), app_user=student, db=db)
    again = await ai_assessor.open_session(_open(s["test"]), app_user=student, db=db)
    assert not first.resumed
    assert again.session_id == first.session_id and again.resumed

    # finished (a record exists): a later open is a new session
    db.add(AiAssessorSessionRecord(session_id=first.session_id, report={}))
    await db.flush()
    after = await ai_assessor.open_session(_open(s["test"]), app_user=student, db=db)
    assert after.session_id != first.session_id and not after.resumed


# --------------------------------------------------------- pending lists
async def test_an_attempt_waiting_for_review_is_not_pending(schema):
    db = schema
    s = await _seed(db, levels=(B.REMEMBER,))
    student = await _targeted_student(db, s)

    listed = await assigned_test.list_my_tests(user=student, session=db)
    assert [t.id for t in listed.items] == [str(s["test"].id)]
    assert len((await student_dashboard.get_student_dashboard(user=student, session=db)).subject_today) == 1

    ai_session = AiAssessorSession(student_id=student.id, test_id=s["test"].id, context_label="x",
                                   duration_seconds=600)
    db.add(ai_session)
    await db.flush()
    db.add(AiAssessorSessionRecord(session_id=ai_session.id, report={"grading_error": "x"}, needs_review=True))
    await db.flush()

    assert (await assigned_test.list_my_tests(user=student, session=db)).items == []
    assert (await student_dashboard.get_student_dashboard(user=student, session=db)).subject_today == []
    # still reachable directly (e.g. from Results)
    assert (await assigned_test.get_my_test(str(s["test"].id), user=student, session=db)).id == str(s["test"].id)


# ------------------------------------------------- spoken-question filter
def test_unspeakable_rejects_questions_that_dont_work_aloud():
    def q(text, qtype=QuestionType.SHORT_ANSWER):
        return type("Q", (), {"text": text, "question_type": qtype})()

    assert voice_test_prep._unspeakable(q("The change in path of light is called ______ of light.")) == "blanks"
    assert voice_test_prep._unspeakable(q("Fill in the blanks: light travels fastest in ...")) == "blanks"
    assert voice_test_prep._unspeakable(q("Draw the ray diagram for an object at 2F.")) == "drawing"
    assert voice_test_prep._unspeakable(q("Read the following text carefully and answer.")) == "passage"
    assert voice_test_prep._unspeakable(q("Study the table below and find the focal length.")) == "passage"
    assert voice_test_prep._unspeakable(q("(i) What is refraction? (ii) State its laws.")) == "multipart"
    assert voice_test_prep._unspeakable(q("State the laws of refraction. OR How is refractive index defined?")) == "multipart"
    assert voice_test_prep._unspeakable(q(" ".join(["word"] * 50) + "?")) == "too_long"
    assert voice_test_prep._unspeakable(q("Balance the chemical equation: Fe + H2O -> Fe3O4 + H2")) == "written_work"
    assert voice_test_prep._unspeakable(q("Match the following metals with their uses.")) == "written_work"
    assert voice_test_prep._unspeakable(q("Tabulate the differences between metals and non-metals.")) == "written_work"
    assert voice_test_prep._unspeakable(q("Fill in: the SI unit of power is ...")) == "blanks"
    assert voice_test_prep._unspeakable(q(
        "Calculate the energy used by a 100 W bulb in 5 hours a day for 30 days.")) == "long_numerical"
    assert voice_test_prep._unspeakable(q("Why does a pencil look bent in a glass of water?")) is None
    assert voice_test_prep._unspeakable(q("Calculate the current when 12 V is applied across 4 ohm.")) is None
    # rewording fixes this one ("tell me two or three ..."), so it stays eligible
    assert voice_test_prep._unspeakable(q("A convex lens has focal length 20 cm. An object is kept 10 cm in front"
                                          " of it. Write any four characteristics of the image formed.")) is None


async def test_prep_swaps_a_question_generation_could_not_reword(schema, monkeypatch):
    db = schema
    _fake_kg(monkeypatch)
    s = await _seed(db, levels=(B.REMEMBER,))
    stuck = await _question(db, s, B.REMEMBER, "Define refraction of light.")
    good = await _question(db, s, B.REMEMBER, "Define refractive index.")

    async def prep(payload):  # rewords everything except `stuck` (falls back to its text)
        return {"questions": [{
            "dataset_question_id": c["id"], "bloom_level": c["bloom_level"],
            "reworded_text": c["text"] if c["id"] == str(stuck.id) else f"In simple words: {c['text']}",
            "spoken_text": c["text"], "expected_points": ["a", "b"], "misconceptions": ["x", "y"], "hints": ["h1", "h2"],
        } for c in payload["candidates"]]}
    monkeypatch.setattr(voice_test_prep.generation_client, "prep_voice_test", prep)
    monkeypatch.setattr(voice_test_prep, "_pick", _pick_in_order(stuck, good))

    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    [q] = await _questions(db, s["test"])
    assert q.dataset_question_id == good.id and q.reworded_text.startswith("In simple words")


async def test_prep_drops_a_question_no_alternative_can_replace(schema, monkeypatch, caplog):
    db = schema
    _fake_kg(monkeypatch)
    s = await _seed(db, levels=(B.REMEMBER, B.UNDERSTAND))
    stuck = await _question(db, s, B.REMEMBER, "Define refraction of light.")
    good = await _question(db, s, B.UNDERSTAND, "Why does a pencil look bent in water?")

    async def prep(payload):  # generation gives `stuck` back in its original wording (its fallback)
        return {"questions": [{
            "dataset_question_id": c["id"], "bloom_level": c["bloom_level"],
            "reworded_text": c["text"] if c["id"] == str(stuck.id) else f"In simple words: {c['text']}",
            "spoken_text": c["text"], "expected_points": ["a", "b"], "misconceptions": ["x", "y"], "hints": ["h1", "h2"],
        } for c in payload["candidates"]]}
    monkeypatch.setattr(voice_test_prep.generation_client, "prep_voice_test", prep)

    with caplog.at_level(logging.WARNING):
        await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    assert [q.dataset_question_id for q in await _questions(db, s["test"])] == [good.id]
    assert s["test"].prep_status == VoiceTestPrepStatus.READY  # the rest still go ahead, no review needed
    assert "voice_test_prep_dropped_unreworded" in caplog.text


def _pick_in_order(*questions):
    """_pick stand-in: the first not-yet-excluded question, in the given order."""
    async def pick(session, test, dataset_ids, level, exclude, topic_id=None):
        return next((q for q in questions if voice_test_prep._uid(q) not in exclude), None)
    return pick


async def test_prep_writes_a_question_for_a_level_the_bank_cant_speak(schema, monkeypatch):
    db = schema
    _fake_kg(monkeypatch)
    _fake_generation(monkeypatch)
    s = await _seed(db, levels=(B.REMEMBER, B.EVALUATE))
    remember = await _question(db, s, B.REMEMBER, "Define refraction of light.")
    await _question(db, s, B.EVALUATE, "Read the following text carefully and answer the questions that follow.")

    from src.services import homework_builder
    sent = {}

    async def sections(session, test, topic_id=None):
        return "Light", [{"title": "Refraction", "text": "Light bends when it enters glass from air."}]

    async def gap_fill(payload):
        sent.update(payload)
        return {"questions": [{"slot_index": 2, "text": "Is it fair to say a lens is useless without refraction? Why?",
                               "answer": "No lens can focus light without refraction."}]}
    monkeypatch.setattr(homework_builder, "_sections", sections)
    monkeypatch.setattr(voice_test_prep.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(voice_test_prep.generation_client, "gap_fill", gap_fill)

    await voice_test_prep.prepare_test(db, s["test"], s["teacher"])

    qs = await _questions(db, s["test"])
    assert [q.bloom_level for q in qs] == [B.REMEMBER, B.EVALUATE]
    assert qs[0].dataset_question_id == remember.id
    assert qs[1].dataset_question_id is None and "lens" in qs[1].original_text
    # 5 planned (Remember x3, Evaluate x2): the bank had one - the rest asked to be written
    assert [slot["bloom_level"] for slot in sent["slots"]] == ["REMEMBER", "REMEMBER", "EVALUATE", "EVALUATE"]
