"""C-012 data fix: the wrong redox explanation on copies of bank question nool-sci-ch01-73 is replaced."""
import importlib.util
import pathlib
import uuid

from sqlalchemy import text

from src.domain.models import BloomLevel, Chapter, DatasetQuestion, PracticeQuestion, QuestionType, Subject

PATH = next(pathlib.Path(__file__).resolve().parents[3].joinpath("database/migrations/versions").glob("*c012_redox*.py"))
spec = importlib.util.spec_from_file_location("c012", PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)  # type: ignore[union-attr]


async def test_wrong_explanation_is_fixed_once_and_only_for_that_bank_question(db_session):
    db = db_session
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db.add(subject)
    await db.flush()
    chapter = Chapter(subject_id=subject.id, name="Chemical Reactions")
    db.add(chapter)
    await db.flush()
    dataset_id = (await db.execute(text("SELECT id FROM datasets LIMIT 1"))).scalar_one()
    owner = (await db.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
    mine = DatasetQuestion(dataset_id=dataset_id, external_uid=migration.EXTERNAL_UID, text="x", answer="a",
                           bloom_level=BloomLevel.UNDERSTAND, question_type=QuestionType.MCQ, created_by=owner)
    db.add(mine)
    await db.flush()
    wrong = "HCl loses electrons as hydrogen goes from +1 to 0 in chlorine formation."
    rows = [PracticeQuestion(chapter_id=chapter.id, bloom_level=BloomLevel.UNDERSTAND, kind="mcq", text="q",
                             options=["a"], correct_index=0, explanation=wrong, origin="bank", used_count=0,
                             expected_points=[], misconceptions=[], source_question_id=sid)
            for sid in (mine.id, None)]
    db.add_all(rows)
    await db.flush()
    sql = migration.FIX.format(table="practice_questions")
    await db.execute(text(sql))
    await db.execute(text(sql))  # idempotent
    await db.refresh(rows[0])
    await db.refresh(rows[1])
    assert "from -1 in HCl to 0 in Cl" in rows[0].explanation
    assert rows[1].explanation == wrong  # a copy of some other question is not touched
