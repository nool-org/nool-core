"""C-005: the 2023 syllabus data fix - out-of-syllabus bank questions dropped, Metals questions
tagged to Chemical Reactions moved, the "Na and K" key fixed."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from src.domain.models import BloomLevel, Chapter, DatasetQuestion, QuestionType, Topic
from tests.test_daily_practice import _seed


def _migration():
    path = Path(__file__).resolve().parents[3] / "database/migrations/versions/f4d6e8a0b2c3_c005_syllabus_and_retag.py"
    spec = importlib.util.spec_from_file_location("c005_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_out_of_syllabus_dropped_metals_retagged_key_fixed(db_session):
    db = db_session
    s = await _seed(db)
    reactions = Chapter(subject_id=s.light.subject_id, name="Chemical Reactions and Equations", order_index=3, grade=10)
    metals = Chapter(subject_id=s.light.subject_id, name="Metals and Non-metals", order_index=4, grade=10)
    db.add_all([reactions, metals])
    await db.flush()
    types_, props, ores = (Topic(chapter_id=c.id, name=n, text_content="") for c, n in (
        (reactions, "1.2 TYPES OF CHEMICAL REACTIONS"), (metals, "3.2 CHEMICAL PROPERTIES OF METALS"),
        (metals, "3.4 OCCURRENCE OF METALS")))
    db.add_all([types_, props, ores])
    await db.flush()

    def q(uid, text, answer="a", chapter=reactions, topic=types_):
        row = DatasetQuestion(dataset_id=s.dataset.id, chapter_id=chapter.id, topic_id=topic.id, text=text,
                              bloom_level=BloomLevel.UNDERSTAND, question_type=QuestionType.SHORT_ANSWER,
                              answer=answer, created_by=s.teacher.id, external_uid=uid)
        db.add(row)
        return row
    periodic = q("CH03:pdf-2700-9", "State Modern Periodic Law.", chapter=metals, topic=props)
    amphoteric = q("CH01:pyq-1302333", "Zinc oxide is an amphoteric oxide. Why?")
    roasting = q("CH01:pyq-1033341", "Roasting vs calcination?")
    stays = q("CH01:other", "Balance the thermit reaction.")
    key = q("CH03:other", "From Be, F, K, Cu: two elements of the same group?",
            answer="K has one electron. Na and K are in the same group.", chapter=metals, topic=props)
    await db.flush()
    ids = {name: row.id for name, row in
           (("periodic", periodic), ("amphoteric", amphoteric), ("roasting", roasting), ("stays", stays), ("key", key))}
    metals_id, reactions_id, props_id, ores_id = metals.id, reactions.id, props.id, ores.id
    dataset_id = s.dataset.id

    def work(conn):
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
    await (await db.connection()).run_sync(work)
    db.expire_all()

    rows = {r.id: r for r in (await db.execute(select(DatasetQuestion).where(
        DatasetQuestion.dataset_id == dataset_id))).scalars()}
    assert ids["periodic"] not in rows
    assert (rows[ids["amphoteric"]].chapter_id, rows[ids["amphoteric"]].topic_id) == (metals_id, props_id)
    assert (rows[ids["roasting"]].chapter_id, rows[ids["roasting"]].topic_id) == (metals_id, ores_id)
    assert rows[ids["stays"]].chapter_id == reactions_id
    assert "Na and K" not in rows[ids["key"]].answer and "Be is in group 2" in rows[ids["key"]].answer
