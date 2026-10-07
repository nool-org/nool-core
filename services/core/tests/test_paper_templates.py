"""Question paper templates (C14): save a paper's blueprint, list it,
use it to start a DRAFT paper, delete authz, and the migrations. The C15
builder (designed templates, scopes, starters, PATCH/duplicate) is in
test_paper_template_builder.py.

Real rows against the dev Postgres via db_session. Until the dev DB has
these migrations, the `schema` fixture creates the table / applies
d7f2b4a6c8e1 inside the test's own transaction (no foreign keys - see
test_voice_test_questions.py) - rolled back with it.
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from shared.errors import ForbiddenError, NotFoundError
from src.api.routes import paper_templates, question_paper
from src.api.schemas.bloom import BloomTarget
from src.api.schemas.paper_templates import CreatePaperTemplateIn, UsePaperTemplateIn
from src.api.schemas.question_paper import CreateQuestionPaperIn, PaperSection
from src.domain.models import (
    Chapter, QuestionPaperStatus, Role, School, SchoolClass, SchoolCurriculum, Subject, TeacherClassAssignment,
    Topic, User, UserStatus,
)
from src.domain.models.paper_template import QuestionPaperTemplate

TABLE = QuestionPaperTemplate.__table__
REVISION = "c5e7a9d1f3b2"
SCOPE_REVISION = "d7f2b4a6c8e1"


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
    insp = inspect(conn)
    if not insp.has_table(TABLE.name):
        conn.execute(CreateTable(TABLE, include_foreign_key_constraints=[]))
    elif "scope" not in {c["name"] for c in insp.get_columns(TABLE.name)}:
        _run(_migration_module(SCOPE_REVISION, "paper_template_scope").upgrade)(conn)


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_table)
    return db_session


def _migration_module(revision=REVISION, slug="question_paper_templates"):
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{revision}_{slug}.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


def _run(step):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def work(conn):
        with Operations.context(MigrationContext.configure(conn)):
            step()
    return work


async def test_migration_upgrade_and_downgrade(db_session):
    module, scope_module = _migration_module(), _migration_module(SCOPE_REVISION, "paper_template_scope")
    assert module.down_revision == "c5e1a7d3f9b2"  # chained after W5-03 at merge
    assert scope_module.down_revision == REVISION

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        if not insp.has_table(TABLE.name):
            return None
        return {c["name"]: c["nullable"] for c in insp.get_columns(TABLE.name)}

    if await db_session.run_sync(columns) is None:  # not applied to this DB yet
        await _ddl(db_session, _run(module.upgrade))  # rolled back with the test's transaction
    if "scope" not in await db_session.run_sync(columns):
        await _ddl(db_session, _run(scope_module.upgrade))
    cols = await db_session.run_sync(columns)
    assert set(cols) == {c.name for c in TABLE.columns}
    assert cols["subject_id"] is True and cols["scope"] is False

    await _ddl(db_session, _run(scope_module.downgrade))
    cols = await db_session.run_sync(columns)
    assert set(cols) == {c.name for c in TABLE.columns} - {"scope"} and cols["subject_id"] is False
    await _ddl(db_session, _run(module.downgrade))
    assert await db_session.run_sync(columns) is None


async def test_scope_migration_keeps_existing_templates_school_wide(schema):
    """Rows saved before scopes were shared with the school - the upgrade
    backfills "school"; new rows default to "personal"."""
    db = schema
    s = await _seed(db)
    out = await _template(db, s)
    await _ddl(db, lambda conn: conn.execute(text("ALTER TABLE question_paper_templates DROP COLUMN scope")))
    await _ddl(db, _run(_migration_module(SCOPE_REVISION, "paper_template_scope").upgrade))
    assert (await db.execute(text("SELECT scope FROM question_paper_templates WHERE id = :id"),
                             {"id": uuid.UUID(out.id)})).scalar_one() == "school"
    default = (await db.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'question_paper_templates' AND column_name = 'scope'"))).scalar_one()
    assert "personal" in default


# ------------------------------------------------------------------ seed
def _user(school_id, role=Role.TEACHER) -> User:
    return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="U",
                role=role, school_id=school_id, status=UserStatus.ACTIVE)


async def _seed(db) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    other_school = School(name=f"Other-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email="c@d.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=10)
    db.add_all([school, other_school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    ch1 = Chapter(subject_id=subject.id, name="Electricity", grade=10)
    ch2 = Chapter(subject_id=subject.id, name="Magnetism", grade=10)
    owner, colleague, admin = _user(school.id), _user(school.id), _user(school.id, Role.SCHOOL_ADMIN)
    outsider = _user(other_school.id)
    db.add_all([section, ch1, ch2, owner, colleague, admin, outsider])
    await db.flush()
    topic = Topic(chapter_id=ch1.id, name="Ohm's law")
    db.add_all([topic, SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True),
                *[TeacherClassAssignment(teacher_id=t.id, class_id=section.id, subject_id=subject.id)
                  for t in (owner, colleague)]])
    await db.flush()
    paper = await question_paper.create_paper(CreateQuestionPaperIn(
        name="Unit Test 1", exam_type="UNIT_TEST", board="CBSE", grade=10, language="English",
        subject_id=str(subject.id), chapter_ids=[str(ch1.id)], topic_ids=[str(topic.id)],
        question_types=["MCQ", "SHORT"], total_marks=20, duration_minutes=45,
        sections=[PaperSection(label="A", question_count=5, marks_each=2),
                  PaperSection(label="B", question_count=2, marks_each=5)],
        bloom_distribution=[BloomTarget(level="APPLY", value=100)],
        instructions=["All questions are compulsory."],
    ), user=owner, session=db)
    return {"school": school, "subject": subject, "ch1": ch1, "ch2": ch2, "topic": topic, "paper": paper,
            "owner": owner, "colleague": colleague, "admin": admin, "outsider": outsider}


async def _template(db, s, name="Unit test pattern"):
    return await paper_templates.create_template(
        CreatePaperTemplateIn(from_paper_id=s["paper"].id, name=name), user=s["owner"], session=db)


# ------------------------------------------------------------------ routes
async def test_create_from_paper_stores_the_blueprint(schema):
    db = schema
    s = await _seed(db)
    out = await _template(db, s)

    assert (out.name, out.exam_type, out.grade, out.subject_id) == (
        "Unit test pattern", "UNIT_TEST", 10, str(s["subject"].id))
    assert (out.total_marks, out.duration_minutes, out.created_by) == (20, 45, str(s["owner"].id))
    row = await db.get(QuestionPaperTemplate, uuid.UUID(out.id))
    assert "name" not in row.blueprint and row.blueprint["chapter_ids"] == [str(s["ch1"].id)]
    assert out.model_dump(by_alias=True)["examType"] == "UNIT_TEST"


async def test_create_needs_a_paper_the_teacher_teaches(schema):
    db = schema
    s = await _seed(db)
    with pytest.raises(NotFoundError):  # another school's teacher can't see it
        await paper_templates.create_template(CreatePaperTemplateIn(from_paper_id=s["paper"].id, name="x"),
                                              user=s["outsider"], session=db)


async def test_saved_from_a_paper_is_personal(schema):
    db = schema
    s = await _seed(db)
    out = await _template(db, s)
    assert (out.scope, out.can_edit, out.can_delete, out.is_starter) == ("personal", True, True, False)

    mine = await paper_templates.list_templates(user=s["owner"], session=db, scope="personal")
    assert [t.id for t in mine.items] == [out.id]
    for viewer in (s["colleague"], s["admin"], s["outsider"]):
        assert (await paper_templates.list_templates(user=viewer, session=db, scope="personal")).items == []
        assert (await paper_templates.list_templates(user=viewer, session=db, scope="school")).items == []


async def test_use_creates_a_draft_with_the_blueprint(schema):
    db = schema
    s = await _seed(db)
    out = await _template(db, s)

    used = await paper_templates.use_template(out.id, UsePaperTemplateIn(name="Unit Test 2"),
                                              user=s["owner"], session=db)
    paper = await question_paper.get_paper(used.paper_id, user=s["owner"], session=db)
    src = s["paper"]
    assert paper.id != src.id and paper.name == "Unit Test 2" and paper.status == QuestionPaperStatus.DRAFT
    for field in ("exam_type", "board", "grade", "language", "subject_id", "chapter_ids", "topic_ids",
                  "question_types", "total_marks", "duration_minutes", "sections", "bloom_distribution",
                  "instructions"):
        assert getattr(paper, field) == getattr(src, field), field

    # new chapters drop the template's topics (they belong to the old chapter)
    used = await paper_templates.use_template(
        out.id, UsePaperTemplateIn(name="Magnetism test", chapter_ids=[str(s["ch2"].id)]),
        user=s["owner"], session=db)
    paper = await question_paper.get_paper(used.paper_id, user=s["owner"], session=db)
    assert (paper.chapter_ids, paper.topic_ids, paper.total_marks) == ([str(s["ch2"].id)], [], 20)


async def test_use_is_school_scoped(schema):
    db = schema
    s = await _seed(db)
    out = await _template(db, s)
    for other in (s["outsider"], s["colleague"]):  # personal: only its creator sees it
        with pytest.raises(NotFoundError):
            await paper_templates.use_template(out.id, UsePaperTemplateIn(name="x"), user=other, session=db)


async def test_delete_only_by_creator_or_school_admin(schema):
    db = schema
    s = await _seed(db)
    first, second = await _template(db, s, "one"), await _template(db, s, "two")
    row = await db.get(QuestionPaperTemplate, uuid.UUID(second.id))
    row.scope = "school"  # a school-wide one (as pre-scope rows are)
    await db.flush()

    for other in (s["colleague"], s["admin"], s["outsider"]):  # can't even see a personal one
        with pytest.raises(NotFoundError):
            await paper_templates.delete_template(first.id, user=other, session=db)
    with pytest.raises(ForbiddenError):
        await paper_templates.delete_template(second.id, user=s["colleague"], session=db)
    listed = await paper_templates.list_templates(user=s["colleague"], session=db, scope="school")
    assert [(t.id, t.can_delete) for t in listed.items] == [(second.id, False)]

    assert (await paper_templates.delete_template(first.id, user=s["owner"], session=db)).status_code == 204
    await paper_templates.delete_template(second.id, user=s["admin"], session=db)
    for scope in ("personal", "school"):
        assert (await paper_templates.list_templates(user=s["owner"], session=db, scope=scope)).items == []
