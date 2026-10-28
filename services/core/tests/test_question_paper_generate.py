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


@pytest.mark.asyncio
async def test_generate_takes_about_a_quarter_from_the_visible_bank_and_kg_writes_the_rest(db_session, monkeypatch):
    """No bank picked on the paper: the teacher's visible QA banks are used,
    BANK_SHARE of the slots are copied from them and only the rest go to kg."""
    from src.domain.models import BloomLevel, DatasetQuestion, PaperDifficultyLevel, QuestionType

    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    bank = Dataset(name="PYQ", question_count=0, description="", subject_id=seeded["subject"].id,
                   board="CBSE", grade=8, type=DatasetType.QA)
    db_session.add(bank)
    await db_session.flush()
    db_session.add(SchoolDataset(school_id=teacher.school_id, dataset_id=bank.id, enabled=True))
    for level in BloomLevel:
        for difficulty in PaperDifficultyLevel:
            db_session.add(DatasetQuestion(
                dataset_id=bank.id, chapter_id=seeded["chapter_id"], bloom_level=level, difficulty=difficulty,
                question_type=QuestionType.MCQ, text=f"Bank {level.value} {difficulty.value} {uuid.uuid4()}?",
                options=["a", "b", "c", "d"], answer="a", created_by=teacher.id))
    await db_session.flush()
    captured: dict = {}

    async def fake_generate_paper_questions(**kwargs):
        captured.update(kwargs)
        return [{"text": f"AI question {i} about algebra?", "answer": "x", "bloom_level": s["bloom_level"],
                 "difficulty": s["difficulty"], "question_type": "mcq", "chapter_number": 3, "topic": "",
                 "options": ["a", "b", "c", "x"]}
                for i, s in enumerate(kwargs["slots"])]

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions",
                        fake_generate_paper_questions)

    await question_paper.generate_paper(seeded["paper_id"], user=teacher, session=db_session)

    questions = (await question_paper.list_paper_questions(seeded["paper_id"], user=teacher, session=db_session)).items
    from_bank = [q for q in questions if q.source == "bank"]
    assert len(questions) == 10 and len(from_bank) == round(10 * question_paper.BANK_SHARE)
    assert captured["total_questions"] == len(captured["slots"]) == 10 - len(from_bank)
    assert all(q.dataset_question_id and q.question_type == "MCQ" for q in from_bank)


@pytest.mark.asyncio
async def test_section_question_types_are_saved_returned_and_used_for_generation(db_session, monkeypatch):
    from src.api.schemas.question_paper import PaperSection

    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    paper = await question_paper.create_paper(
        CreateQuestionPaperIn(
            name="Sectioned", exam_type="UNIT_TEST", board="CBSE", grade=8, language="English",
            subject_id=str(seeded["subject"].id), chapter_ids=[str(seeded["chapter_id"])],
            question_types=["MCQ", "Short Answer I"], total_marks=4,
            sections=[PaperSection(label="A", question_count=2, marks_each=1, question_types=["Short Answer I"]),
                      PaperSection(label="B", question_count=1, marks_each=2)],
        ),
        user=teacher, session=db_session,
    )
    out = await question_paper.get_paper(paper.id, user=teacher, session=db_session)
    assert [s.question_types for s in out.sections] == [["Short Answer I"], None]

    captured: dict = {}

    async def fake_generate_paper_questions(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions",
                        fake_generate_paper_questions)
    monkeypatch.setattr(question_paper, "_bank_slots", lambda *a, **k: _no_bank())
    await question_paper.generate_paper(paper.id, user=teacher, session=db_session)
    # section A asked for short answers even for its 1-mark slots; B falls back to the paper's types
    assert [s["question_type"] for s in captured["slots"]] == ["short_answer", "short_answer", "short_answer"]


async def _no_bank():
    return {}


@pytest.mark.asyncio
async def test_fill_missing_keeps_what_the_paper_has_and_milo_writes_only_the_rest(db_session, monkeypatch):
    """PaperNeedsFix's "Let Milo write the missing ones": a short paper keeps
    its questions; kg writes just the empty slots, with no bank pass."""
    from shared.errors import ConflictError, ForbiddenError, NotFoundError

    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    short = {"n": 4}  # the first run comes back 6 short of 10
    asked = []

    async def fake_generate_paper_questions(**kwargs):
        asked.append(len(kwargs["slots"]))
        n = short.pop("n", len(kwargs["slots"]))
        return [{"text": f"AI question {len(asked)}.{i}?", "answer": "x", "bloom_level": s["bloom_level"],
                 "difficulty": s["difficulty"], "question_type": "mcq", "chapter_number": 3, "topic": "",
                 "options": ["a", "b", "c", "x"]}
                for i, s in enumerate(kwargs["slots"][:n])]

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions",
                        fake_generate_paper_questions)
    out = await question_paper.generate_paper(seeded["paper_id"], user=teacher, session=db_session)
    assert any("6 of the blueprint's 10" in i for i in out.validation.issues)
    first = [q.text for q in (await question_paper.list_paper_questions(
        seeded["paper_id"], user=teacher, session=db_session)).items]

    stranger = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                    role=Role.TEACHER, school_id=teacher.school_id, status=UserStatus.ACTIVE)
    db_session.add(stranger)
    await db_session.flush()
    with pytest.raises((ForbiddenError, NotFoundError)):
        await question_paper.fill_missing(seeded["paper_id"], user=stranger, session=db_session)

    out = await question_paper.fill_missing(seeded["paper_id"], user=teacher, session=db_session)
    questions = (await question_paper.list_paper_questions(seeded["paper_id"], user=teacher, session=db_session)).items
    assert asked[-1] == 6 and len(questions) == 10
    assert [q.text for q in questions[:4]] == first  # kept, in order
    assert [q.order for q in questions] == list(range(1, 11))
    assert out.status == "REVIEW" and not any("couldn't be filled" in i for i in out.validation.issues)

    with pytest.raises(ConflictError):  # nothing left to fill
        await question_paper.fill_missing(seeded["paper_id"], user=teacher, session=db_session)


@pytest.mark.asyncio
async def test_kg_questions_land_by_slot_index_and_an_mcq_without_options_is_not_saved(db_session, monkeypatch):
    """C-009/AI-009: kg answers out of order and one MCQ has no options - each
    question keeps its own slot's marks and the optionless MCQ is dropped."""
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    monkeypatch.setattr(question_paper, "_bank_slots", lambda *a, **k: _no_bank())
    slots_seen = []

    async def fake_generate_paper_questions(**kwargs):
        slots_seen.extend(kwargs["slots"])
        out = [{"text": f"Q for slot {i}?", "answer": "x", "bloom_level": s["bloom_level"], "difficulty": s["difficulty"],
                "question_type": "short_answer", "chapter_number": 3, "topic": "", "slot_index": i}
               for i, s in enumerate(kwargs["slots"])]
        out[1].update(question_type="mcq", options=None)
        return list(reversed(out))

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions",
                        fake_generate_paper_questions)
    await question_paper.generate_paper(seeded["paper_id"], user=teacher, session=db_session)
    questions = (await question_paper.list_paper_questions(seeded["paper_id"], user=teacher, session=db_session)).items
    expected = [i for i in range(len(slots_seen)) if i != 1]
    assert [q.text for q in questions] == [f"Q for slot {i}?" for i in expected]
    assert [q.marks for q in questions] == [slots_seen[i]["marks"] for i in expected]
