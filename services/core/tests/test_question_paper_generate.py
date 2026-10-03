"""POST /question-papers/{id}/generate must tell the kg service which
Curriculum root to query (subject/board/grade) - kg_client previously
dropped these fields entirely, so the kg service silently fell back to
its own defaults (CBSE/Grade 10/Science) for every paper regardless of
its actual subject. See kg_client.py's docstring on generate_paper_questions.
"""

import uuid

import pytest

from src.api.routes import question_paper
from src.api.schemas.dataset import DatasetShare
from src.api.schemas.question_paper import CreateQuestionPaperIn
from src.domain.models import (
    Chapter, Dataset, DatasetType, Role, School, SchoolClass, SchoolCurriculum, SchoolDataset, Subject, TeacherClassAssignment,
    User, UserStatus,
)


async def _seed_paper_ready_to_generate(db_session) -> tuple[User, dict]:
    school = School(name="Generate Test School", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()

    subject = Subject(name=f"Mathematics-{uuid.uuid4()}", grade=8)
    db_session.add(subject)
    await db_session.flush()

    chapter = Chapter(subject_id=subject.id, name="Algebra", order_index=3)
    db_session.add(chapter)
    await db_session.flush()

    teacher = User(
        firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    section = SchoolClass(school_id=school.id, grade=8, section="A")
    db_session.add_all([teacher, section])
    await db_session.flush()
    db_session.add_all([TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                        SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db_session.flush()

    paper = await question_paper.create_paper(
        CreateQuestionPaperIn(
            name="Midterm", exam_type="UNIT_TEST", board="ICSE", grade=8,
            language="English", subject_id=str(subject.id), chapter_ids=[str(chapter.id)],
        ),
        user=teacher, session=db_session,
    )
    return teacher, {"paper_id": paper.id, "subject": subject, "chapter_id": chapter.id}


@pytest.mark.asyncio
async def test_generate_paper_passes_the_papers_own_subject_board_and_grade_to_the_kg_service(
    db_session, monkeypatch,
):
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    captured: dict = {}

    async def fake_generate_paper_questions(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "src.api.routes.question_paper.kg_client.generate_paper_questions",
        fake_generate_paper_questions,
    )

    await question_paper.generate_paper(seeded["paper_id"], user=teacher, session=db_session)

    assert captured["subject"] == seeded["subject"].name
    assert captured["board"] == "ICSE"
    assert captured["grade"] == 8


@pytest.mark.asyncio
async def test_generate_paper_prefers_its_shared_datasets_board_over_its_own(
    db_session, monkeypatch,
):
    """A Dataset *is* a KG root (e.g. "8th Maths, CBSE") - if the paper shares
    from one that has a board set, that's the authoritative scope, even
    though the paper's own (denormalized-at-creation) board says something
    else. Its grade must be the paper's (a mismatch is refused at update)."""
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    kg_dataset = Dataset(
        name="8th Maths", question_count=0, description="",
        subject_id=seeded["subject"].id, board="CBSE", grade=8, type=DatasetType.PRIMARY_CONTENT,
    )
    db_session.add(kg_dataset)
    await db_session.flush()
    db_session.add(SchoolDataset(school_id=teacher.school_id, dataset_id=kg_dataset.id, enabled=True))
    await db_session.flush()

    await question_paper.update_paper(
        seeded["paper_id"],
        CreateQuestionPaperIn(
            name="Midterm", exam_type="UNIT_TEST", board="ICSE", grade=8,
            language="English", subject_id=str(seeded["subject"].id),
            chapter_ids=[str(seeded["chapter_id"])],
            dataset_shares=[DatasetShare(dataset_id=str(kg_dataset.id), percent=100)],
        ),
        user=teacher, session=db_session,
    )

    captured: dict = {}

    async def fake_generate_paper_questions(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "src.api.routes.question_paper.kg_client.generate_paper_questions",
        fake_generate_paper_questions,
    )

    await question_paper.generate_paper(seeded["paper_id"], user=teacher, session=db_session)

    assert captured["board"] == "CBSE"
    assert captured["grade"] == 8
