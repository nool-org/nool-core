"""Paper Copilot (C12): chat / quick actions / voice -> validated proposals,
chat history, explain-this-question, and the paper_chat_messages migration.
The generation service is faked (monkeypatched client).

Real rows against the dev Postgres via db_session; until the dev DB has
this item's migration the `schema` fixture creates paper_chat_messages
(without foreign keys) inside the test's own transaction - same pattern as
test_voice_test_questions.py.
"""

import asyncio
import base64
import importlib.util
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from shared.errors import ConflictError, ForbiddenError, ValidationError
from src.api.routes import paper_copilot as routes
from src.api.routes import question_paper
from src.api.schemas.dataset import DatasetShare
from src.api.schemas.paper_copilot import PaperChatIn
from src.api.schemas.question_paper import CreateQuestionPaperIn
from src.domain.models import (
    BloomLevel, Chapter, Dataset, DatasetQuestion, QuestionPaperQuestion, QuestionPaperStatus, QuestionType, Role,
    School, SchoolClass, SchoolCurriculum, SchoolDataset, Subject, TeacherClassAssignment, Topic, User, UserStatus,
)
from src.domain.models.paper_chat import PaperChatMessage
from src.domain.models.question_paper import PaperDifficultyLevel
from src.services import paper_copilot
from src.services.generation_client import GenerationServiceUnavailableError

B = BloomLevel
REVISION = "c5e2a7d1f3b8"
pytestmark = pytest.mark.asyncio


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


def _create_table(conn):
    if not inspect(conn).has_table(PaperChatMessage.__tablename__):
        conn.execute(CreateTable(PaperChatMessage.__table__, include_foreign_key_constraints=[]))


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_table)
    return db_session


def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_paper_chat_messages.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w5_02_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "c1f5a9e3b7d2"  # chained after W5-01 at merge

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                step()
        return work

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        return {c["name"] for c in insp.get_columns("paper_chat_messages")} if insp.has_table("paper_chat_messages") else None

    if await db_session.run_sync(columns) is None:  # not applied to this DB yet
        await _ddl(db_session, run(module.upgrade))
    assert await db_session.run_sync(columns) == {c.name for c in PaperChatMessage.__table__.columns}
    await _ddl(db_session, run(module.downgrade))
    assert await db_session.run_sync(columns) is None


# ------------------------------------------------------------------ seed
async def _seed(db) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=10)
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Light - Reflection and Refraction", order_index=9, grade=10)
    other_chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    db.add_all([section, chapter, other_chapter, teacher])
    await db.flush()
    topic = Topic(chapter_id=chapter.id, name="Refraction")
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=subject.id, grade=10, board="CBSE")
    db.add_all([topic, dataset,
                TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db.flush()
    db.add(SchoolDataset(school_id=school.id, dataset_id=dataset.id, enabled=True))
    await db.flush()

    paper_out = await question_paper.create_paper(CreateQuestionPaperIn(
        name="Unit test", exam_type="UNIT_TEST", board="CBSE", grade=10, language="English",
        subject_id=str(subject.id), chapter_ids=[str(chapter.id)],
        dataset_shares=[DatasetShare(dataset_id=str(dataset.id), percent=100)], total_marks=10,
    ), user=teacher, session=db)
    paper = await question_paper._get_paper_in_school(db, paper_out.id, school.id)
    paper.status = QuestionPaperStatus.REVIEW
    questions = []
    for order, (level, txt) in enumerate([(B.REMEMBER, "Define refraction."), (B.REMEMBER, "State Snell's law."),
                                          (B.UNDERSTAND, "Why does a pencil look bent in water?")], start=1):
        q = QuestionPaperQuestion(paper_id=paper.id, order=order, bloom_level=level, marks=2, text=txt)
        db.add(q)
        questions.append(q)
    s = {"school": school, "teacher": teacher, "paper": paper, "chapter": chapter, "other_chapter": other_chapter,
         "topic": topic, "dataset": dataset, "questions": questions}
    s["bank"] = [
        await _bank(db, s, B.REMEMBER, "Define refraction."),  # already on the paper -> never a candidate
        await _bank(db, s, B.REMEMBER, "What is the refractive index?"),
        await _bank(db, s, B.APPLY, "Light enters glass (n = 1.5) at 30 degrees. Find the angle of refraction."),
        await _bank(db, s, B.ANALYZE, "Compare refraction in glass and water using refractive indices."),
    ]
    return s


async def _bank(db, s, level, txt) -> DatasetQuestion:
    q = DatasetQuestion(dataset_id=s["dataset"].id, chapter_id=s["chapter"].id, topic_id=s["topic"].id,
                        bloom_level=level, difficulty=PaperDifficultyLevel.MEDIUM,
                        question_type=QuestionType.SHORT_ANSWER, text=txt, answer=f"answer to {txt}",
                        created_by=s["teacher"].id, external_uid=f"uid-{uuid.uuid4()}")
    db.add(q)
    await db.flush()
    return q


def _fake_plan(monkeypatch, result=None, *, error=None):
    calls = []

    async def fake(payload):
        calls.append(payload)
        if error:
            raise error
        return result(payload) if callable(result) else (result or {"reply": "Nothing to change.", "ops": []})

    monkeypatch.setattr(paper_copilot.paper_copilot_client, "plan", fake)
    return calls


def _new(chapter_id, *, source="ai", dataset_question_id=None, marks=3, **kw):
    return {"text": "A ray of light passes from air into water at 45 degrees. Explain what happens.",
            "question_type": "SHORT_ANSWER", "marks": marks, "bloom_level": "APPLY", "difficulty": "HARD",
            "chapter_id": chapter_id, "topic_id": None, "options": None, "answer": "It bends towards the normal.",
            "source": source, "dataset_question_id": dataset_question_id, **kw}


async def _chat(s, db, **body):
    return await routes.chat(str(s["paper"].id), PaperChatIn(**body), user=s["teacher"], session=db)


# ------------------------------------------------------------------ chat
async def test_quick_action_sends_its_key_and_prefers_higher_bloom_bank_questions_not_on_the_paper(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    calls = _fake_plan(monkeypatch)
    out = await _chat(s, db, quick_action="harder")

    payload = calls[0]
    assert payload["quick_action"] == "harder" and payload["message"] is None
    assert (payload["board"], payload["grade"], payload["total_marks"]) == ("CBSE", 10, 10)
    assert [q["number"] for q in payload["questions"]] == [1, 2, 3]
    texts = [c["text"] for c in payload["bank_candidates"]]
    assert "Define refraction." not in texts and len(texts) == 3
    assert {c["bloom_level"] for c in payload["bank_candidates"][:2]} == {"APPLY", "ANALYZE"}
    assert (out.reply, out.proposals, out.transcript) == ("Nothing to change.", [], None)

    history = await routes.chat_history(str(s["paper"].id), user=s["teacher"], session=db)
    assert [(m.role, m.text) for m in history.messages] == [("teacher", "Make it harder"), ("copilot", "Nothing to change.")]


async def test_bank_first_proposals_and_invalid_ops_are_dropped(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    q1, q2, q3 = (str(q.id) for q in s["questions"])
    chapter = str(s["chapter"].id)

    def plan(payload):
        bank_id = next(c["id"] for c in payload["bank_candidates"] if c["bloom_level"] == "APPLY")
        return {"reply": "Here you go.", "ops": [
            {"op": "replace", "question_id": q3, "reason": "harder, from the bank",
             "new_question": _new(chapter, source="bank", dataset_question_id=bank_id, marks=2)},
            {"op": "add", "section_name": None, "position": None, "reason": "application question",
             "new_question": _new(chapter)},
            {"op": "edit", "question_id": q2, "reason": "more marks", "changes": {"marks": 3, "text": None}},
            # dropped:
            {"op": "remove", "question_id": str(uuid.uuid4()), "reason": "unknown id"},
            {"op": "remove", "question_id": q3, "reason": "q3 already replaced"},
            {"op": "add", "reason": "bank id not offered",
             "new_question": _new(chapter, source="bank", dataset_question_id=str(uuid.uuid4()))},
            {"op": "add", "reason": "chapter not on the paper", "new_question": _new(str(s["other_chapter"].id))},
            {"op": "add", "section_name": "Section Z", "reason": "no such section", "new_question": _new(chapter)},
            {"op": "edit", "question_id": q1, "reason": "nothing", "changes": {}},
            {"op": "reorder", "ordered_question_ids": [q2, q1], "reason": "missing q3"},
            {"op": "add", "reason": "bad enum", "new_question": _new(chapter, bloom_level="GENIUS")},
            {"op": "explode", "reason": "unknown op"},
        ]}

    _fake_plan(monkeypatch, plan)
    out = await _chat(s, db, message="Make question 3 harder and add one 3-mark application question on refraction")

    assert [p.op.op for p in out.proposals] == ["replace", "add", "edit"]
    replace, add, edit = out.proposals
    assert replace.before.question_id == q3 and replace.before.text == "Why does a pencil look bent in water?"
    assert (replace.after.source, replace.after.marks, replace.reason) == ("bank", 2, "harder, from the bank")
    assert add.before is None and add.after.source == "ai" and add.after.marks == 3
    assert (edit.after.source, edit.after.marks, edit.after.text) == ("edited", 3, "State Snell's law.")
    assert len({p.proposal_id for p in out.proposals}) == 3

    # stored with the copilot message, camelCase C11 on the wire; edit carries only the set changes
    history = await routes.chat_history(str(s["paper"].id), user=s["teacher"], session=db)
    stored = history.messages[-1].proposals
    assert stored[0]["op"]["newQuestion"]["source"] == "bank" and stored[0]["op"]["questionId"] == q3
    assert stored[2]["op"] == {"op": "edit", "questionId": q2, "changes": {"marks": 3}}
    # proposals are not applied
    await db.refresh(s["questions"][2])
    assert s["questions"][2].text == "Why does a pencil look bent in water?"


async def test_planner_failure_is_a_friendly_reply_not_a_500(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    _fake_plan(monkeypatch, error=GenerationServiceUnavailableError("down"))
    out = await _chat(s, db, message="add a question")
    assert out.reply == paper_copilot.FRIENDLY_FAILURE and out.proposals == []

    _fake_plan(monkeypatch, {"ops": "garbage"})  # malformed response
    out = await _chat(s, db, message="again")
    assert out.reply == paper_copilot.FRIENDLY_FAILURE


async def test_voice_note_is_transcribed_with_chapter_vocabulary_and_used_as_the_message(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    seen = {}

    async def fake_transcribe(payload):
        seen.update(payload)
        return {"text": " make question 3 harder "}

    monkeypatch.setattr(paper_copilot.paper_copilot_client, "transcribe", fake_transcribe)
    calls = _fake_plan(monkeypatch)
    audio = base64.b64encode(b"fake-audio").decode()
    out = await _chat(s, db, audio_base64=audio)
    assert out.transcript == "make question 3 harder" and calls[0]["message"] == "make question 3 harder"
    assert seen["audio_base64"] == audio and "Light - Reflection and Refraction" in seen["prompt"]

    with pytest.raises(ValidationError):
        await _chat(s, db, audio_base64=base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode())
    with pytest.raises(ValidationError):
        await _chat(s, db, audio_base64="not base64!!")

    async def broken(payload):
        raise GenerationServiceUnavailableError("down")

    monkeypatch.setattr(paper_copilot.paper_copilot_client, "transcribe", broken)
    out = await _chat(s, db, audio_base64=audio)
    assert out.transcript is None and out.proposals == [] and "type it" in out.reply


async def test_history_is_sent_to_the_planner_and_capped(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    calls = _fake_plan(monkeypatch)
    for i in range(11):
        await _chat(s, db, message=f"message {i}")
    assert calls[1]["history"] == [{"role": "teacher", "text": "message 0"}, {"role": "copilot", "text": "Nothing to change."}]
    assert len(calls[-1]["history"]) == 20 and calls[-1]["history"][-2]["text"] == "message 9"


async def test_empty_request_finalized_paper_and_other_teachers_are_refused(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    _fake_plan(monkeypatch)
    with pytest.raises(ValidationError):
        await _chat(s, db, message="   ")
    outsider = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="O",
                    role=Role.TEACHER, school_id=s["school"].id, status=UserStatus.ACTIVE)
    db.add(outsider)
    await db.flush()
    with pytest.raises(ForbiddenError):
        await routes.chat(str(s["paper"].id), PaperChatIn(message="hi"), user=outsider, session=db)
    s["paper"].status = QuestionPaperStatus.FINALIZED
    with pytest.raises(ConflictError):
        await _chat(s, db, message="hi")


# ------------------------------------------------------------------ explain
async def test_explain_gives_the_why_and_a_bank_alternative_at_the_same_level(schema):
    db = schema
    s = await _seed(db)
    q = s["questions"][1]  # REMEMBER, 2 marks
    out = await routes.explain_question(str(s["paper"].id), str(q.id), user=s["teacher"], session=db)
    assert out.bloom_level == B.REMEMBER and out.chapter_name == "Light - Reflection and Refraction"
    assert "2-mark" in out.why and "Remember" in out.why and "recall" in out.why
    alt = out.alternative
    assert alt.op.op == "replace" and alt.op.question_id == str(q.id)
    assert alt.op.new_question.text == "What is the refractive index?"  # the bank question already on the paper is skipped
    assert (alt.op.new_question.source, alt.op.new_question.marks) == ("bank", 2)
    assert alt.before.text == "State Snell's law."

    lone = s["questions"][2]  # UNDERSTAND - no bank question at that level
    assert (await routes.explain_question(str(s["paper"].id), str(lone.id), user=s["teacher"],
                                          session=db)).alternative is None
