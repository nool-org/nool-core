"""T-065 / T-067: the list is newest first and carries createdAt; the owner teacher can rename, duplicate and
delete a paper (nobody else can); T-066: a paper name is at most 120 characters."""

import uuid

import pytest
from pydantic import ValidationError as PydanticError
from sqlalchemy import func, select

from shared.errors import ForbiddenError, NotFoundError
from src.api.routes import paper_answer_key as key_routes
from src.api.routes import question_paper
from src.api.schemas.paper_answer_key import UpdateAnswerKeyIn, MarkingPointIO
from src.api.schemas.question_paper import CreateQuestionPaperIn, RenamePaperIn
from src.domain.models import (
    QuestionPaper, QuestionPaperQuestion, QuestionPaperSection, QuestionPaperStatus, Role, SchoolClass,
    TeacherClassAssignment, User, UserStatus, BloomLevel,
)
from tests.test_question_paper_generate import _seed_paper_ready_to_generate


async def _with_question(db, seeded):
    paper = await db.get(QuestionPaper, uuid.UUID(seeded["paper_id"]))
    q = QuestionPaperQuestion(paper_id=paper.id, order=1, bloom_level=BloomLevel.REMEMBER, marks=1, text="What is x?")
    db.add(q)
    paper.status = QuestionPaperStatus.REVIEW
    await db.flush()
    return q


async def _colleague(db, teacher, seeded):
    other = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="Colleague",
                 role=Role.TEACHER, school_id=teacher.school_id, status=UserStatus.ACTIVE)
    db.add(other)
    await db.flush()
    section = (await db.execute(select(SchoolClass).where(SchoolClass.school_id == teacher.school_id))).scalars().first()
    db.add(TeacherClassAssignment(teacher_id=other.id, class_id=section.id, subject_id=seeded["subject"].id))
    await db.flush()
    return other


@pytest.mark.asyncio
async def test_list_is_newest_first_with_created_at(db_session):
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    newer = await question_paper.create_paper(CreateQuestionPaperIn(
        name="Newer", exam_type="UNIT_TEST", board="ICSE", grade=8, language="English",
        subject_id=str(seeded["subject"].id), chapter_ids=[str(seeded["chapter_id"])]), user=teacher, session=db_session)
    older = await db_session.get(QuestionPaper, uuid.UUID(seeded["paper_id"]))
    older.created_at = (await db_session.get(QuestionPaper, uuid.UUID(newer.id))).created_at.replace(year=2020)
    await db_session.flush()
    items = (await question_paper.list_papers(user=teacher, session=db_session)).items
    mine = [p for p in items if p.id in (newer.id, seeded["paper_id"])]
    assert [p.id for p in mine] == [newer.id, seeded["paper_id"]] and all(p.created_at for p in mine)


@pytest.mark.asyncio
async def test_owner_renames_duplicates_and_deletes_a_paper_others_cannot(db_session):
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    q = await _with_question(db_session, seeded)
    pid = seeded["paper_id"]
    await key_routes.update_answer_key(pid, str(q.id), UpdateAnswerKeyIn(
        answer="x is 2", marking_points=[MarkingPointIO(point="x is 2", marks=1)]), user=teacher, session=db_session)

    renamed = await question_paper.rename_paper(pid, RenamePaperIn(name="  Term 1  "), user=teacher, session=db_session)
    assert renamed.name == "Term 1"

    copy = await question_paper.duplicate_paper(pid, user=teacher, session=db_session)
    assert copy.id != pid and copy.name == "Term 1 (copy)" and copy.status == QuestionPaperStatus.REVIEW
    copied = (await question_paper.list_paper_questions(copy.id, user=teacher, session=db_session)).items
    assert [c.text for c in copied] == ["What is x?"] and copied[0].id != str(q.id)

    other = await _colleague(db_session, teacher, seeded)
    for call in (question_paper.rename_paper(pid, RenamePaperIn(name="Mine"), user=other, session=db_session),
                 question_paper.duplicate_paper(pid, user=other, session=db_session),
                 question_paper.delete_paper(pid, user=other, session=db_session)):
        with pytest.raises(ForbiddenError):
            await call

    await question_paper.delete_paper(pid, user=teacher, session=db_session)
    with pytest.raises(NotFoundError):
        await question_paper.get_paper(pid, user=teacher, session=db_session)
    left = await db_session.scalar(select(func.count()).select_from(QuestionPaperQuestion).where(
        QuestionPaperQuestion.paper_id == uuid.UUID(pid)))
    assert left == 0
    # the copy is a paper of its own and survives
    assert (await question_paper.get_paper(copy.id, user=teacher, session=db_session)).name == "Term 1 (copy)"


def test_paper_names_are_capped_at_120_characters():
    with pytest.raises(PydanticError):
        RenamePaperIn(name="x" * 121)
    assert RenamePaperIn(name="x" * 120)
