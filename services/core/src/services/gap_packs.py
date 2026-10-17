"""Gap packs: every homework question the generation service writes is kept in
a shared bank, "Nool Gap Packs - <Subject>", tagged with its chapter, topic,
Bloom level and difficulty. The next student who needs that topic at that level
- in any class, in any school - gets it from the bank instead of paying for it
again, so each question is paid for once. A school uses the pack as soon as it
teaches the subject (enable_for_school), like any assigned catalog dataset."""

import hashlib
import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    BloomLevel,
    Dataset,
    DatasetQuestion,
    DatasetType,
    PaperDifficultyLevel,
    QuestionType,
    SchoolDataset,
    Subject,
)

PREFIX = "Nool Gap Packs - "


def uid_of(text: str) -> str:
    """Same question, same uid - however its whitespace or case came out."""
    return "gap-" + hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode()).hexdigest()[:20]


async def pack_of(session: AsyncSession, subject_id, *, create: bool, grade: int | None = None) -> Dataset | None:
    subject = await session.get(Subject, subject_id)
    if subject is None:
        return None
    name = PREFIX + subject.name
    found = (await session.execute(select(Dataset).where(
        Dataset.name == name, Dataset.subject_id == subject.id, Dataset.school_id.is_(None)))).scalar_one_or_none()
    if found is not None or not create:
        return found
    pack = Dataset(name=name, question_count=0, type=DatasetType.QA, subject_id=subject.id,
                   grade=subject.grade or grade,
                   board="CBSE", restricted=False,
                   description=f"Homework questions Nool has written for {subject.name} - kept so every school reuses them")
    session.add(pack)
    await session.flush()
    return pack


async def enable_for_school(session: AsyncSession, subject_id, school_id) -> None:
    """The subject's gap pack, switched on for this school (a no-op once it is)."""
    pack = await pack_of(session, subject_id, create=False)
    if pack is None or school_id is None:
        return
    await session.execute(pg_insert(SchoolDataset).values(
        id=uuid.uuid4(), school_id=school_id, dataset_id=pack.id, enabled=True,
    ).on_conflict_do_nothing(index_elements=["school_id", "dataset_id"]))


async def keep(session: AsyncSession, *, subject_id, school_id, grade, chapter_id, topic_id, created_by,
               bloom_level: BloomLevel, difficulty: PaperDifficultyLevel | None,
               text: str, options: list[str] | None, answer: str, solution: str | None) -> DatasetQuestion:
    """Stores one written question in the subject's gap pack (once - a repeat returns
    the stored row) and makes sure this school uses the pack."""
    pack = await pack_of(session, subject_id, create=True, grade=grade)
    uid = uid_of(text)
    await session.execute(pg_insert(DatasetQuestion).values(
        id=uuid.uuid4(), dataset_id=pack.id, chapter_id=chapter_id, topic_id=topic_id, created_by=created_by,
        bloom_level=bloom_level, difficulty=difficulty, text=text, options=options or None, answer=answer,
        solution=solution or None, external_uid=uid,
        question_type=QuestionType.MCQ if options else QuestionType.SHORT_ANSWER,
        source={"dataset": "gap-pack"},
    ).on_conflict_do_nothing(index_elements=["dataset_id", "external_uid"]))
    await enable_for_school(session, subject_id, school_id)
    return (await session.execute(select(DatasetQuestion).where(
        DatasetQuestion.dataset_id == pack.id, DatasetQuestion.external_uid == uid))).scalar_one()
