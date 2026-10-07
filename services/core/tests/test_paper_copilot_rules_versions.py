"""Paper Copilot (W5-01): blueprint additions (C9), the rules check (C10),
structured actions + versions + restore (C11) and question source badges.

Real rows against the dev Postgres via db_session. Until the dev DB has
this item's migration, the `schema` fixture adds the new
question_paper_questions columns and creates question_paper_versions
(without foreign keys) inside the test's own transaction, so it's all
rolled back - same pattern as test_homework_answers.py.
"""

import asyncio
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import TypeAdapter
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from src.api.routes import paper_rules as rules_routes
from src.api.routes import paper_versions, question_paper
from src.api.schemas.question_paper import (
    BloomTargetPercent,
    CreateQuestionPaperIn,
    DifficultyMix,
    PaperActionsIn,
    PaperOp,
    PaperSection,
    UpdateQuestionPaperQuestionIn,
)
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Dataset,
    DatasetQuestion,
    DatasetType,
    Homework,
    HomeworkDifficulty,
    HomeworkQuestion,
    HomeworkStatus,
    QuestionPaper,
    QuestionPaperStatus,
    QuestionType,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestQuestion,
)
from src.domain.models.question_paper import PaperDifficultyLevel, QuestionPaperQuestion, QuestionPaperVersion
from src.services import paper_actions
from src.services.paper_repeats import normalize
from src.services.paper_rules import estimate_minutes

B = BloomLevel
REVISION = "c1f5a9e3b7d2"
OPS = TypeAdapter(list[PaperOp])


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
            if code not in ("40P01", "55P03") or attempt == attempts - 1:  # deadlock / lock timeout
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


def _create_new_schema(conn):
    conn.execute(text(
        "ALTER TABLE question_paper_questions "
        "ADD COLUMN IF NOT EXISTS question_type question_type, "
        "ADD COLUMN IF NOT EXISTS difficulty paper_difficulty_level, "
        "ADD COLUMN IF NOT EXISTS chapter_id uuid, ADD COLUMN IF NOT EXISTS topic_id uuid, "
        "ADD COLUMN IF NOT EXISTS section_name varchar, ADD COLUMN IF NOT EXISTS options jsonb, "
        "ADD COLUMN IF NOT EXISTS answer text, ADD COLUMN IF NOT EXISTS source varchar DEFAULT 'ai', "
        "ADD COLUMN IF NOT EXISTS dataset_question_id uuid"
    ))
    if not inspect(conn).has_table(QuestionPaperVersion.__table__.name):
        conn.execute(CreateTable(QuestionPaperVersion.__table__, include_foreign_key_constraints=[]))


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_new_schema)
    return db_session


def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_paper_copilot_versions.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w5_01_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "b4d8e2f6a1c9"

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                step()
        return work

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        return ({c["name"] for c in insp.get_columns("question_paper_questions")},
                {c["name"] for c in insp.get_columns("question_paper_versions")}
                if insp.has_table("question_paper_versions") else None)

    if (await db_session.run_sync(columns))[1] is None:  # not applied to this DB yet
        await _ddl(db_session, run(module.upgrade))  # rolled back with the test's transaction
    qpq, versions = await db_session.run_sync(columns)
    assert {c.name for c in QuestionPaperQuestion.__table__.columns} <= qpq
    assert versions == {c.name for c in QuestionPaperVersion.__table__.columns}

    await _ddl(db_session, run(module.downgrade))
    qpq, versions = await db_session.run_sync(columns)
    assert "source" not in qpq and "section_name" not in qpq and versions is None


def test_routes_are_registered():
    from src.main import create_app

    paths = {(r.path, m) for r in create_app().routes for m in getattr(r, "methods", ())}
    for path, method in [
        ("/api/v1/question-papers/{paper_id}/rules-check", "GET"),
        ("/api/v1/question-papers/{paper_id}/actions", "POST"),
        ("/api/v1/question-papers/{paper_id}/versions", "GET"),
        ("/api/v1/question-papers/{paper_id}/versions/{version_id}/restore", "POST"),
    ]:
        assert (path, method) in paths


def test_estimate_minutes_table_and_normalize():
    class Q:
        def __init__(self, t, m):
            self.question_type, self.marks = t, m

    T = QuestionType
    # 1.5 + 1 + 2*3 + 2.5*5 + 2*2 = 25
    assert estimate_minutes([Q(T.MCQ, 1), Q(T.TRUE_FALSE, 1), Q(T.SHORT_ANSWER, 3), Q(T.LONG_ANSWER, 5), Q(None, 2)]) == 25
    assert estimate_minutes([Q(T.MCQ, 1)]) == 2  # rounded up
    assert normalize("  What is   Ohm's law?! ") == "what is ohm s law"


# ------------------------------------------------------------------ seed
async def _seed(db, *, grade=10):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    other_subject = Subject(name=f"History-{uuid.uuid4()}")
    db.add_all([school, subject, other_subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=grade, section=uuid.uuid4().hex[:8].upper())
    ch1 = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=grade)
    ch2 = Chapter(subject_id=subject.id, name="Magnetism", order_index=12, grade=grade)
    foreign = Chapter(subject_id=other_subject.id, name="Mughals", order_index=1, grade=grade)
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    outsider = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="O",
                    role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    db.add_all([section, ch1, ch2, foreign, teacher, outsider,
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db.flush()
    topic = Topic(chapter_id=ch1.id, name="Ohm's law")
    db.add_all([topic, TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id)])
    await db.flush()
    return dict(school=school, subject=subject, cls=section, ch1=ch1, ch2=ch2, foreign=foreign, topic=topic,
                teacher=teacher, outsider=outsider)


def _blueprint(s, **over) -> CreateQuestionPaperIn:
    body = dict(
        name="Unit test", exam_type="UNIT_TEST", board="CBSE", grade=10, language="English",
        subject_id=str(s["subject"].id), chapter_ids=[str(s["ch1"].id), str(s["ch2"].id)],
        sections=[PaperSection(label="A", question_count=2, marks_each=1),
                  PaperSection(label="B", question_count=1, marks_each=3)],
        total_marks=5, duration_minutes=60,
        bloom_targets=[BloomTargetPercent(level=B.REMEMBER, percent=40), BloomTargetPercent(level=B.APPLY, percent=60)],
        difficulty_mix=DifficultyMix(easy=40, medium=60, hard=0),
    )
    body.update(over)
    return CreateQuestionPaperIn(**body)


async def _generated_paper(db, monkeypatch, s, kg_questions=None, references=()):
    async def fake_generate(**kw):
        fake_generate.refs = kw["reference_questions"]
        return kg_questions if kg_questions is not None else [
            {"text": "Define electric current.", "answer": "Rate of flow of charge", "bloom_level": "REMEMBER",
             "difficulty": "EASY", "question_type": "SHORT_ANSWER", "chapter_number": 11, "topic": "Ohm's law"},
            {"text": "State Ohm's law.", "answer": "V = IR", "bloom_level": "REMEMBER", "difficulty": "EASY",
             "question_type": "SHORT_ANSWER", "chapter_number": 11, "topic": "Ohm's law"},
            {"text": "Design a circuit with two resistors in parallel.", "answer": "...", "bloom_level": "APPLY",
             "difficulty": "MEDIUM", "question_type": "LONG_ANSWER", "chapter_number": 12, "topic": "x"},
        ]

    async def fake_refs(session, paper):
        return list(references)

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions", fake_generate)
    monkeypatch.setattr(question_paper, "_dataset_reference_questions", fake_refs)
    paper = await question_paper.create_paper(_blueprint(s), user=s["teacher"], session=db)
    paper = await question_paper.generate_paper(paper.id, user=s["teacher"], session=db)
    assert paper.status == QuestionPaperStatus.REVIEW
    return paper, fake_generate


def _new_q(s, **over) -> dict:
    q = {"text": "Explain how a fuse protects a circuit.", "questionType": "SHORT_ANSWER", "marks": 1,
         "bloomLevel": "UNDERSTAND", "difficulty": "MEDIUM", "chapterId": str(s["ch1"].id), "topicId": None,
         "options": None, "answer": "It melts", "source": "ai", "datasetQuestionId": None}
    q.update(over)
    return q


async def _act(db, s, paper_id, ops, origin="manual"):
    body = PaperActionsIn(ops=OPS.validate_python(ops), summary="test", origin=origin)
    return await paper_versions.post_actions(paper_id, body, user=s["teacher"], session=db)


async def _questions(db, paper_id):
    return [paper_actions.question_out(q) for q in await paper_actions.load_questions(db, paper_id)]


# ------------------------------------------------------------------ C9
async def test_blueprint_bloom_targets_and_difficulty_mix_round_trip(schema):
    db = schema
    s = await _seed(db)
    out = await question_paper.create_paper(_blueprint(s), user=s["teacher"], session=db)
    assert {(t.level, t.percent) for t in out.bloom_targets} == {(B.REMEMBER, 40), (B.APPLY, 60)}
    assert {(t.level, t.value) for t in out.bloom_distribution} == {(B.REMEMBER, 40), (B.APPLY, 60)}
    assert out.difficulty_mix == DifficultyMix(easy=40, medium=60, hard=0)
    assert out.duration_minutes == 60
    dumped = out.model_dump(by_alias=True)
    assert "bloomTargets" in dumped and "difficultyMix" in dumped and "durationMinutes" in dumped


# ------------------------------------------------------------------ generate
async def test_generate_stores_metadata_bank_source_and_version_1(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=s["subject"].id, grade=10,
                      board="CBSE", type=DatasetType.QA)
    db.add(dataset)
    await db.flush()
    bank = DatasetQuestion(dataset_id=dataset.id, chapter_id=s["ch1"].id, bloom_level=B.REMEMBER,
                           question_type=QuestionType.SHORT_ANSWER, text="State Ohm's law.", answer="V=IR",
                           created_by=s["teacher"].id)
    db.add(bank)
    await db.flush()
    bank_id = bank.id
    paper, fake = await _generated_paper(db, monkeypatch, s, references=[
        {"id": bank_id, "text": "state ohm's law", "answer": "V=IR", "options": None, "bloom_level": "REMEMBER",
         "difficulty": "EASY", "question_type": "SHORT_ANSWER"}])
    assert all("id" not in r for r in fake.refs)  # bank ids never go to the kg service

    qs = await _questions(db, paper.id)
    assert [q.section_name for q in qs] == ["A", "A", "B"]
    assert [q.marks for q in qs] == [1, 1, 3]
    assert [q.source for q in qs] == ["ai", "bank", "ai"]
    assert qs[1].dataset_question_id == str(bank_id) and qs[0].dataset_question_id is None
    assert qs[0].chapter_id == str(s["ch1"].id) and qs[0].topic_id == str(s["topic"].id)
    assert qs[2].chapter_id == str(s["ch2"].id) and qs[2].topic_id is None
    assert qs[0].question_type == QuestionType.SHORT_ANSWER and qs[0].difficulty == PaperDifficultyLevel.EASY

    versions = (await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items
    assert [(v.number, v.origin) for v in versions] == [(1, "generate")]
    snap = (await db.get(QuestionPaperVersion, uuid.UUID(versions[0].id))).snapshot
    assert [q["text"] for q in snap] == [q.text for q in qs]


# ------------------------------------------------------------------ C10
async def test_rules_check_math_on_the_generated_paper(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    rc = await rules_routes.get_rules_check(paper.id, user=s["teacher"], session=db)

    assert rc.ok and (rc.total_marks.target, rc.total_marks.actual) == (5, 5)
    assert [(x.name, x.target_marks, x.actual_marks, x.target_count, x.actual_count, x.ok) for x in rc.sections] == [
        ("A", 2, 2, 2, 2, True), ("B", 3, 3, 1, 1, True)]
    assert {(b.level, b.target_percent, b.actual_percent) for b in rc.bloom_mix} == {
        (B.REMEMBER, 40, 40), (B.APPLY, 60, 60)}
    assert [(c.name, c.question_count, c.marks) for c in rc.chapters] == [("Electricity", 2, 2), ("Magnetism", 1, 3)]
    assert rc.difficulty == DifficultyMix(easy=40, medium=60, hard=0)
    assert rc.estimated_minutes == 12 and rc.duration_minutes == 60  # 2 + 2 (short, 1 mark) + 7.5 (long, 3) -> 12
    assert rc.issues == [] and rc.repeats == []


async def test_rules_check_flags_marks_sections_bloom_chapters_difficulty_and_time(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    qs = await _questions(db, paper.id)
    # drop the only Magnetism question; the remaining 2 marks are all REMEMBER/EASY
    out = await _act(db, s, paper.id, [{"op": "remove", "questionId": qs[2].id}])
    await question_paper.update_paper(paper.id, _blueprint(s, duration_minutes=1), user=s["teacher"], session=db)
    # update_paper resets the paper to DRAFT - the questions are still there
    rc = await rules_routes.get_rules_check(paper.id, user=s["teacher"], session=db)
    codes = {i.code: i for i in rc.issues}
    assert not rc.ok
    assert codes["total_marks_mismatch"].severity == "error"
    assert codes["section_mismatch"].severity == "error"
    assert codes["bloom_off_target"].severity == "warning"
    assert codes["chapter_uncovered"].severity == "warning" and "Magnetism" in codes["chapter_uncovered"].message
    assert codes["difficulty_off_target"].severity == "warning"
    assert codes["over_duration"].severity == "warning"
    assert rc.difficulty == DifficultyMix(easy=100, medium=0, hard=0)
    assert out.rules_check.total_marks.actual == 2


async def test_repeats_across_papers_homework_and_voice_tests(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=s["subject"].id, grade=10,
                      board="CBSE", type=DatasetType.QA)
    db.add(dataset)
    await db.flush()
    vt = VoiceTest(class_id=s["cls"].id, subject_id=s["subject"].id, chapter_id=s["ch1"].id, duration_minutes=10,
                   completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS)
    old = QuestionPaper(school_id=s["school"].id, created_by=s["teacher"].id, name="Last term", exam_type="X",
                        board="CBSE", grade=10, language="English", subject_id=s["subject"].id)
    ancient = QuestionPaper(school_id=s["school"].id, created_by=s["teacher"].id, name="Two years ago",
                            exam_type="X", board="CBSE", grade=10, language="English", subject_id=s["subject"].id,
                            created_at=datetime.now(timezone.utc) - timedelta(days=400))
    db.add_all([vt, old, ancient])
    await db.flush()
    hw = Homework(test_id=vt.id, class_id=s["cls"].id, gap_topic="Ohm practice", gap_mastery_percent=40,
                  total_questions=1, difficulty=HomeworkDifficulty.MIXED,
                  target_mode=AssignmentTargetMode.WHOLE_CLASS, status=HomeworkStatus.ASSIGNED)
    db.add(hw)
    await db.flush()
    db.add_all([
        QuestionPaperQuestion(paper_id=old.id, order=1, bloom_level=B.REMEMBER, marks=1, text="DEFINE electric current!"),
        QuestionPaperQuestion(paper_id=ancient.id, order=1, bloom_level=B.APPLY, marks=3,
                              text="Design a circuit with two resistors in parallel."),
        HomeworkQuestion(homework_id=hw.id, order=1, bloom_level=B.REMEMBER, dataset_id=dataset.id,
                         text="State Ohms law", answer="V=IR"),
        VoiceTestQuestion(voice_test_id=vt.id, order=1, bloom_level=B.APPLY, original_text="Something unrelated",
                          reworded_text="r", spoken_text="s"),
    ])
    await db.flush()

    paper, _ = await _generated_paper(db, monkeypatch, s)
    qs = await _questions(db, paper.id)
    # voice-test question that matches the paper's 3rd question exactly
    db.add(VoiceTestQuestion(voice_test_id=vt.id, order=2, bloom_level=B.APPLY, reworded_text="r", spoken_text="s",
                             original_text="design a circuit with two resistors in parallel"))
    await db.flush()

    rc = await rules_routes.get_rules_check(paper.id, user=s["teacher"], session=db)
    found = {r.question_id: (r.matched_in, r.matched_title) for r in rc.repeats}
    assert found == {
        qs[0].id: ("paper", "Last term"),        # exact after normalizing
        qs[1].id: ("homework", "Ohm practice"),  # "State Ohm's law." ~ "State Ohms law" (ratio >= 0.85)
        qs[2].id: ("voice_test", "Electricity"),  # the 2-year-old paper is out of the window
    }
    assert {i.question_id for i in rc.issues if i.code == "repeat"} == set(found)


# ------------------------------------------------------------------ C11
async def test_actions_apply_replace_add_edit_remove_reorder_with_sources_and_versions(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    q1, q2, q3 = await _questions(db, paper.id)

    out = await _act(db, s, paper.id, [
        {"op": "replace", "questionId": q1.id, "newQuestion": _new_q(s, source="bank", text="Bank question")},
        {"op": "edit", "questionId": q2.id, "changes": {"text": "State Ohm's law with units.", "marks": 2}},
        {"op": "add", "sectionName": "B", "position": None, "newQuestion": _new_q(
            s, text="Pick the unit of resistance.", questionType="MCQ", options=["ohm", "volt", "amp", "watt"],
            topicId=str(s["topic"].id), marks=3, bloomLevel="APPLY")},
        {"op": "remove", "questionId": q3.id},
    ], origin="chat")
    assert out.version_number == 2
    texts = [q.text for q in out.questions]
    assert texts == ["Bank question", "State Ohm's law with units.", "Pick the unit of resistance."]
    assert [q.source for q in out.questions] == ["bank", "edited", "ai"]
    assert [q.section_name for q in out.questions] == ["A", "A", "B"]
    assert [q.order for q in out.questions] == [1, 2, 3]
    assert out.rules_check.total_marks.actual == 6

    ids = [q.id for q in out.questions]
    out = await _act(db, s, paper.id, [{"op": "reorder", "orderedQuestionIds": list(reversed(ids))}])
    assert [q.id for q in out.questions] == list(reversed(ids)) and out.version_number == 3
    versions = (await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items
    assert [(v.number, v.origin, v.created_by) for v in versions] == [
        (3, "manual", str(s["teacher"].id)), (2, "chat", str(s["teacher"].id)), (1, "generate", str(s["teacher"].id))]


@pytest.mark.parametrize("make_op, error", [
    (lambda s, q: {"op": "remove", "questionId": str(uuid.uuid4())}, NotFoundError),
    (lambda s, q: {"op": "edit", "questionId": q.id, "changes": {"marks": 0}}, ValidationError),
    (lambda s, q: {"op": "edit", "questionId": q.id, "changes": {}}, ValidationError),
    (lambda s, q: {"op": "edit", "questionId": q.id, "changes": {"questionType": "MCQ"}}, ValidationError),
    (lambda s, q: {"op": "replace", "questionId": q.id, "newQuestion": _new_q(s, marks=-1)}, ValidationError),
    (lambda s, q: {"op": "replace", "questionId": q.id,
                   "newQuestion": _new_q(s, chapterId=str(s["foreign"].id))}, ValidationError),
    (lambda s, q: {"op": "replace", "questionId": q.id,
                   "newQuestion": _new_q(s, chapterId=str(s["ch2"].id), topicId=str(s["topic"].id))}, ValidationError),
    (lambda s, q: {"op": "add", "newQuestion": _new_q(s, questionType="MCQ", options=["a", "b", "c"])},
     ValidationError),
    (lambda s, q: {"op": "add", "sectionName": "Z", "newQuestion": _new_q(s)}, ValidationError),
    (lambda s, q: {"op": "add", "position": 9, "newQuestion": _new_q(s)}, ValidationError),
    (lambda s, q: {"op": "add", "newQuestion": _new_q(s, datasetQuestionId=str(uuid.uuid4()))}, ValidationError),
    (lambda s, q: {"op": "reorder", "orderedQuestionIds": [q.id]}, ValidationError),
])
async def test_a_bad_op_rolls_back_the_whole_call(schema, monkeypatch, make_op, error):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    before = await _questions(db, paper.id)
    with pytest.raises(error):
        await _act(db, s, paper.id, [
            {"op": "edit", "questionId": before[0].id, "changes": {"text": "changed first"}},
            make_op(s, before[1]),
        ])
    after = await _questions(db, paper.id)
    assert [(q.id, q.text, q.source) for q in after] == [(q.id, q.text, q.source) for q in before]
    assert len((await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items) == 1


def test_enum_validation_happens_in_the_schema():
    with pytest.raises(Exception):
        OPS.validate_python([{"op": "edit", "questionId": "x", "changes": {"bloomLevel": "GENIUS"}}])
    with pytest.raises(Exception):
        OPS.validate_python([{"op": "explode", "questionId": "x"}])


async def test_restore_creates_a_new_version_equal_to_the_snapshot(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    original = await _questions(db, paper.id)
    await _act(db, s, paper.id, [{"op": "remove", "questionId": original[0].id},
                                 {"op": "edit", "questionId": original[1].id, "changes": {"text": "edited"}}])
    v1 = (await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items[-1]

    out = await paper_versions.restore(paper.id, v1.id, user=s["teacher"], session=db)
    assert out.version_number == 3
    assert [q.model_dump() for q in out.questions] == [q.model_dump() for q in original]  # ids kept too
    versions = (await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items
    assert (versions[0].origin, versions[0].summary) == ("restore", "Restored version 1")
    with pytest.raises(NotFoundError):
        await paper_versions.restore(paper.id, str(uuid.uuid4()), user=s["teacher"], session=db)


async def test_patch_text_edit_marks_edited_and_creates_a_version(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    q = (await _questions(db, paper.id))[0]
    out = await question_paper.update_paper_question(
        paper.id, q.id, UpdateQuestionPaperQuestionIn(text="Teacher wording"), user=s["teacher"], session=db)
    assert (out.items[0].text, out.items[0].source) == ("Teacher wording", "edited")
    versions = (await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items
    assert [(v.number, v.origin) for v in versions] == [(2, "manual"), (1, "generate")]


async def test_finalized_paper_blocks_actions_and_restore(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    q = (await _questions(db, paper.id))[0]
    v1 = (await paper_versions.list_versions(paper.id, user=s["teacher"], session=db)).items[0]
    await question_paper.finalize_paper(paper.id, user=s["teacher"], session=db)
    with pytest.raises(ConflictError) as exc:
        await _act(db, s, paper.id, [{"op": "remove", "questionId": q.id}])
    assert exc.value.status_code == 409
    with pytest.raises(ConflictError):
        await paper_versions.restore(paper.id, v1.id, user=s["teacher"], session=db)


async def test_preview_ops_reports_the_rules_check_but_keeps_nothing(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    before = await _questions(db, paper.id)
    row = await db.get(QuestionPaper, uuid.UUID(paper.id))
    rc = await paper_actions.preview_ops(db, row, OPS.validate_python([{"op": "remove", "questionId": before[2].id}]))
    assert rc.total_marks.actual == 2 and not rc.ok
    assert [q.id for q in await _questions(db, paper.id)] == [q.id for q in before]


async def test_only_a_teacher_of_the_paper_may_use_the_endpoints(schema, monkeypatch):
    db = schema
    s = await _seed(db)
    paper, _ = await _generated_paper(db, monkeypatch, s)
    for call in (
        rules_routes.get_rules_check(paper.id, user=s["outsider"], session=db),
        paper_versions.list_versions(paper.id, user=s["outsider"], session=db),
        paper_versions.post_actions(paper.id, PaperActionsIn(ops=OPS.validate_python(
            [{"op": "reorder", "orderedQuestionIds": []}]), summary="x", origin="manual"),
            user=s["outsider"], session=db),
    ):
        with pytest.raises(ForbiddenError):
            await call
