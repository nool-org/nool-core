"""Answer key + marking scheme, and Set B (C13): the migration, bank answer
reuse, marks-sum validation, teacher edits protected from regeneration, Set B
slot equivalence with no identical/near-identical texts, and clear errors (no
fake content) when the generation service is unavailable.

Real rows against the dev Postgres via db_session. Until the dev DB has this
item's migration, the `schema` fixture creates the new tables inside the
test's own transaction (without foreign keys - see test_voice_test_questions.py).
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from shared.errors import ConflictError, ForbiddenError, ValidationError
from src.api.routes import paper_answer_key as key_routes
from src.api.routes import paper_parallel as parallel_routes
from src.api.routes import question_paper
from src.api.schemas.dataset import DatasetShare
from src.api.schemas.paper_answer_key import CreateParallelIn, MarkingPointIO, UpdateAnswerKeyIn
from src.api.schemas.question_paper import CreateQuestionPaperIn, PaperSection
from src.domain.models import (
    BloomLevel, Chapter, Dataset, DatasetQuestion, QuestionPaper, QuestionPaperChapter, QuestionPaperQuestion,
    QuestionPaperStatus, QuestionType, Role, School, SchoolClass, SchoolCurriculum, SchoolDataset, Subject,
    TeacherClassAssignment, Topic, User, UserStatus,
)
from src.domain.models.paper_answer_key import QuestionPaperAnswerKey, QuestionPaperParallel
from src.services import paper_answer_key, paper_parallel
from src.services.generation_client import GenerationServiceUnavailableError
from src.services.paper_parallel import too_similar

B = BloomLevel
NEW_TABLES = [QuestionPaperAnswerKey.__table__, QuestionPaperParallel.__table__]
REVISION = "c5e1a7d3f9b2"


async def _ddl(db, work, attempts=10):  # other runs share the dev DB
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


def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_paper_answer_keys_and_parallels.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w5_03_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "c5e2a7d1f3b8"  # chained after W5-02 at merge

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                step()
        return work

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        return {t.name: ({c["name"] for c in insp.get_columns(t.name)} if insp.has_table(t.name) else None)
                for t in NEW_TABLES}

    if (await db_session.run_sync(columns))["question_paper_answer_keys"] is None:  # not applied yet
        await _ddl(db_session, run(module.upgrade))  # rolled back with the test's transaction
    found = await db_session.run_sync(columns)
    for table in NEW_TABLES:
        assert found[table.name] == {c.name for c in table.columns}

    await _ddl(db_session, run(module.downgrade))
    assert set((await db_session.run_sync(columns)).values()) == {None}


# ------------------------------------------------------------------ seed
async def _teacher(db, school_id) -> User:
    t = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
             role=Role.TEACHER, school_id=school_id, status=UserStatus.ACTIVE)
    db.add(t)
    await db.flush()
    return t


async def _seed(db, questions=(("What is Ohm's law?", B.REMEMBER, 1), ("A 220 V supply drives 5 A. Find R.", B.APPLY, 3)),
                with_bank=True) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=10)
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    db.add_all([section, chapter])
    await db.flush()
    teacher = await _teacher(db, school.id)
    topic = Topic(chapter_id=chapter.id, name="Ohm's law", text_content="V = IR. Resistance is measured in ohm.")
    db.add_all([topic, TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    dataset = Dataset(name="Important", question_count=0, description="", subject_id=subject.id, grade=10)
    db.add(dataset)
    await db.flush()
    db.add(SchoolDataset(school_id=school.id, dataset_id=dataset.id, enabled=True))
    await db.flush()
    out = await question_paper.create_paper(
        CreateQuestionPaperIn(name="Unit Test 1", exam_type="UNIT_TEST", board="CBSE", grade=10, language="English",
                              subject_id=str(subject.id), chapter_ids=[str(chapter.id)], total_marks=4,
                              sections=[PaperSection(label="A", question_count=2, marks_each=2)],
                              dataset_shares=[DatasetShare(dataset_id=str(dataset.id), percent=100)] if with_bank else None),
        user=teacher, session=db)
    paper = await db.get(QuestionPaper, uuid.UUID(out.id))
    rows = [QuestionPaperQuestion(paper_id=paper.id, order=i, bloom_level=b, marks=m, text=t)
            for i, (t, b, m) in enumerate(questions, start=1)]
    db.add_all(rows)
    paper.status = QuestionPaperStatus.REVIEW
    await db.flush()
    return {"school": school, "teacher": teacher, "paper": paper, "questions": rows, "dataset": dataset,
            "chapter": chapter, "topic": topic, "subject": subject}


async def _bank(db, s, level, text_, *, marks=None, answer=None, solution=None) -> DatasetQuestion:
    q = DatasetQuestion(dataset_id=s["dataset"].id, chapter_id=s["chapter"].id, topic_id=s["topic"].id,
                        bloom_level=level, question_type=QuestionType.SHORT_ANSWER, text=text_, marks=marks,
                        answer=answer or f"answer to {text_}", solution=solution, created_by=s["teacher"].id)
    db.add(q)
    await db.flush()
    return q


def _fake_key(monkeypatch, calls, transform=None):
    async def fake(payload):
        calls.append(payload)
        items = [{"id": i["id"], "answer": f"key: {i['text']}",
                  "marking_points": [{"point": "value point", "marks": i["marks"]}]} for i in payload["items"]]
        return {"items": transform(items) if transform else items, "failed_ids": []}
    monkeypatch.setattr(paper_answer_key.client, "is_configured", lambda: True)
    monkeypatch.setattr(paper_answer_key.client, "answer_key", fake)


async def _get(db, s):
    return await key_routes.get_answer_key(str(s["paper"].id), user=s["teacher"], session=db)


# ------------------------------------------------------------- answer key
async def test_generate_uses_the_bank_answer_and_drops_schemes_that_dont_add_up(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    bank = await _bank(db, s, B.REMEMBER, "What is Ohm's law?", answer="V is proportional to I", solution="At constant T.")
    s["questions"][0].dataset_question_id = bank.id  # a W5-01 column; a plain attribute until then
    calls = []

    def break_second(items):
        items[1]["marking_points"] = [{"point": "half", "marks": 1}]  # question is worth 3
        return items
    _fake_key(monkeypatch, calls, break_second)

    out = await key_routes.generate_answer_key(str(s["paper"].id), user=s["teacher"], session=db)

    first, second = calls[0]["items"]
    assert (first["reference_answer"], first["reference_solution"]) == ("V is proportional to I", "At constant T.")
    assert second["reference_answer"] is None and second["marks"] == 3 and second["bloom_level"] == "APPLY"
    assert (calls[0]["board"], calls[0]["grade"], calls[0]["subject"]) == ("CBSE", 10, s["subject"].name)
    assert [i.question_id for i in out.items] == [str(s["questions"][0].id)]  # bad-sum scheme never stored
    assert out.items[0].total_marks == 1 and out.items[0].marking_points[0].marks == 1
    assert out.model_dump(by_alias=True)["items"][0].keys() == {"questionId", "answer", "markingPoints", "totalMarks"}


async def test_teacher_edit_is_protected_from_regeneration_unless_overwrite(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    q1, q2 = s["questions"]
    calls = []
    _fake_key(monkeypatch, calls)
    pid = str(s["paper"].id)
    await key_routes.generate_answer_key(pid, user=s["teacher"], session=db)

    edited = await key_routes.update_answer_key(
        pid, str(q2.id), UpdateAnswerKeyIn(answer="R = 44 ohm", marking_points=[
            MarkingPointIO(point="formula", marks=1), MarkingPointIO(point="substitution", marks=1),
            MarkingPointIO(point="answer with unit", marks=1)]),
        user=s["teacher"], session=db)
    assert edited.answer == "R = 44 ohm" and len(edited.marking_points) == 3

    await key_routes.generate_answer_key(pid, user=s["teacher"], session=db)  # nothing missing
    assert len(calls) == 1
    await key_routes.generate_answer_key(pid, all=True, user=s["teacher"], session=db)
    assert [i["id"] for i in calls[1]["items"]] == [str(q1.id)]  # AI-written only
    assert {i.question_id: i.answer for i in (await _get(db, s)).items}[str(q2.id)] == "R = 44 ohm"

    await key_routes.generate_answer_key(pid, overwrite=True, user=s["teacher"], session=db)
    assert len(calls[2]["items"]) == 2
    assert {i.question_id: i.answer for i in (await _get(db, s)).items}[str(q2.id)].startswith("key: ")


async def test_patch_validates_the_marks_sum_and_needs_an_answer_for_a_new_key(schema):
    db = schema
    s = await _seed(db)
    pid, q2 = str(s["paper"].id), str(s["questions"][1].id)
    with pytest.raises(ValidationError, match="add up"):
        await key_routes.update_answer_key(pid, q2, UpdateAnswerKeyIn(answer="x", marking_points=[
            MarkingPointIO(point="a", marks=1), MarkingPointIO(point="b", marks=1.5)]), user=s["teacher"], session=db)
    with pytest.raises(ValidationError, match="send an answer"):
        await key_routes.update_answer_key(pid, q2, UpdateAnswerKeyIn(), user=s["teacher"], session=db)
    out = await key_routes.update_answer_key(pid, q2, UpdateAnswerKeyIn(answer="44 ohm", marking_points=[
        MarkingPointIO(point="a", marks=1.5), MarkingPointIO(point="b", marks=1.5)]), user=s["teacher"], session=db)
    assert out.total_marks == 3 and [p.marks for p in out.marking_points] == [1.5, 1.5]
    key = await db.get(QuestionPaperAnswerKey, s["questions"][1].id)
    assert key.updated_by == s["teacher"].id


async def test_generate_without_the_generation_service_is_a_clear_error(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    monkeypatch.setattr(paper_answer_key.client, "is_configured", lambda: False)
    with pytest.raises(GenerationServiceUnavailableError):
        await key_routes.generate_answer_key(str(s["paper"].id), user=s["teacher"], session=db)
    assert (await _get(db, s)).items == []


async def test_only_a_teacher_of_the_paper_may_use_the_answer_key(schema):
    db = schema
    s = await _seed(db)
    stranger = await _teacher(db, s["school"].id)  # same school, doesn't teach it
    with pytest.raises(ForbiddenError):
        await key_routes.get_answer_key(str(s["paper"].id), user=stranger, session=db)
    with pytest.raises(ForbiddenError):
        await parallel_routes.create_parallel(str(s["paper"].id), None, user=stranger, session=db)


# ------------------------------------------------------------------ Set B
def test_similarity_catches_copies_and_near_copies_only():
    assert too_similar("What is Ohm's law?", ["what is  ohm's LAW?"])
    assert too_similar("What is Ohm's law ?", ["What is Ohm's law?"])
    assert not too_similar("State the SI unit of resistance.", ["What is Ohm's law?"])


def _fake_gap_fill(monkeypatch, calls, text_for=lambda slot, i: f"Generated {slot['bloom_level']} question {i}"):
    async def fake(payload):
        calls.append(payload)
        return {"questions": [{"slot_index": i, "question_type": "short_answer", "text": text_for(slot, i),
                               "options": [], "answer": "a", "solution": "", "grounded_in": "Ohm's law"}
                              for i, slot in enumerate(payload["slots"])], "unfilled_slot_indexes": [], "attempts": 1}
    monkeypatch.setattr(paper_parallel.client, "is_configured", lambda: True)
    monkeypatch.setattr(paper_parallel.client, "gap_fill", fake)


async def _questions(db, paper_id):
    return list((await db.execute(select(QuestionPaperQuestion).where(QuestionPaperQuestion.paper_id == paper_id)
                                  .order_by(QuestionPaperQuestion.order))).scalars().all())


async def test_set_b_takes_bank_alternatives_first_then_generates_with_equivalent_slots(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    await _bank(db, s, B.REMEMBER, "What is Ohm's law?")         # identical - excluded
    await _bank(db, s, B.REMEMBER, "What is Ohm's law ?", marks=1)  # near-identical - excluded
    await _bank(db, s, B.REMEMBER, "Define resistance.", marks=5)  # wrong marks - excluded
    alt = await _bank(db, s, B.REMEMBER, "State the SI unit of resistance.", marks=1)
    await _bank(db, s, B.UNDERSTAND, "Why do wires heat up?", marks=3)  # wrong Bloom level
    calls = []
    _fake_gap_fill(monkeypatch, calls)

    out = await parallel_routes.create_parallel(str(s["paper"].id), None, user=s["teacher"], session=db)

    assert out.name == "Unit Test 1 - Set B"
    new = await db.get(QuestionPaper, uuid.UUID(out.paper_id))
    assert new.status == QuestionPaperStatus.DRAFT and new.created_by == s["teacher"].id and new.total_marks == 4
    chapters = (await db.execute(select(QuestionPaperChapter.chapter_id).where(
        QuestionPaperChapter.paper_id == new.id))).scalars().all()
    assert chapters == [s["chapter"].id]
    rows = await _questions(db, new.id)
    originals = s["questions"]
    assert [(r.order, r.bloom_level, r.marks) for r in rows] == [(q.order, q.bloom_level, q.marks) for q in originals]
    assert rows[0].text == alt.text
    assert rows[1].text == "Generated APPLY question 0"
    assert [s_["bloom_level"] for s_ in calls[0]["slots"]] == ["APPLY"] and calls[0]["sections"][0]["title"] == "Ohm's law"
    assert set(calls[0]["avoid"]) >= {q.text for q in originals}
    assert not {r.text for r in rows} & {q.text for q in originals}
    link = await db.get(QuestionPaperParallel, new.id)
    assert link.parallel_of == s["paper"].id


async def test_set_b_never_keeps_a_generated_copy_of_the_original(schema, monkeypatch):
    db = schema
    s = await _seed(db, with_bank=False)
    before = (await db.execute(select(func.count()).select_from(QuestionPaper))).scalar_one()
    _fake_gap_fill(monkeypatch, [], text_for=lambda slot, i: "What is Ohm's law?" if i == 0 else "Fresh one.")
    with pytest.raises(ConflictError, match="1 of 2"):
        await parallel_routes.create_parallel(str(s["paper"].id), CreateParallelIn(name="B"), user=s["teacher"],
                                              session=db)
    assert (await db.execute(select(func.count()).select_from(QuestionPaper))).scalar_one() == before


async def test_set_b_without_bank_or_generation_is_a_clear_error(schema, monkeypatch):
    db = schema
    s = await _seed(db, with_bank=False)
    monkeypatch.setattr(paper_parallel.client, "is_configured", lambda: False)
    before = (await db.execute(select(func.count()).select_from(QuestionPaper))).scalar_one()
    with pytest.raises(GenerationServiceUnavailableError, match="2 slot"):
        await parallel_routes.create_parallel(str(s["paper"].id), None, user=s["teacher"], session=db)
    assert (await db.execute(select(func.count()).select_from(QuestionPaper))).scalar_one() == before


async def test_set_b_uses_the_given_name_and_needs_questions(schema, monkeypatch):
    db = schema
    s = await _seed(db, questions=())
    with pytest.raises(ConflictError, match="no questions"):
        await parallel_routes.create_parallel(str(s["paper"].id), None, user=s["teacher"], session=db)
    s = await _seed(db, questions=(("What is Ohm's law?", B.REMEMBER, 1),))
    _fake_gap_fill(monkeypatch, [])
    out = await parallel_routes.create_parallel(str(s["paper"].id), CreateParallelIn(name="Retest"),
                                                user=s["teacher"], session=db)
    assert out.name == "Retest"
