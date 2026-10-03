"""services/homework_builder.py - bank first, generation for the gaps,
legacy fallback when the generation service is unavailable."""

import uuid

import pytest

from src.domain.models import (
    AssignmentTargetMode, BloomLevel, Chapter, Dataset, DatasetQuestion, Homework, HomeworkDifficulty,
    HomeworkQuestion, HomeworkStatus, QuestionType, Role, School, SchoolClass, SchoolCurriculum, SchoolDataset, Subject,
    TeacherClassAssignment,
    Topic, User, UserStatus, VoiceTest,
)
from src.domain.models.question_paper import PaperDifficultyLevel as D
from src.services import generation_client
from src.services.homework_builder import build_homework_questions

B = BloomLevel


async def _seed(db_session, bank_size=3) -> dict:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    section = SchoolClass(school_id=school.id, grade=10, section="A")
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    db_session.add_all([section, chapter, teacher])
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="11.6 Resistors", text_content="In series R = R1 + R2 ...")
    db_session.add_all([topic, TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id)])
    await db_session.flush()
    imp = Dataset(name="Important", question_count=0, description="", subject_id=subject.id, grade=10, board="CBSE")
    pyq = Dataset(name="PYQ", question_count=0, description="", subject_id=subject.id, grade=10, board="CBSE")
    db_session.add_all([imp, pyq])
    await db_session.flush()
    db_session.add_all([SchoolDataset(school_id=school.id, dataset_id=d.id, enabled=True) for d in (imp, pyq)])
    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True))
    bank = [DatasetQuestion(dataset_id=imp.id, chapter_id=chapter.id, topic_id=topic.id, bloom_level=B.APPLY,
                            difficulty=D.EASY, question_type=QuestionType.SHORT_ANSWER, text=f"bank q{i}",
                            answer="a", created_by=teacher.id, external_uid=f"uid-{uuid.uuid4()}")
            for i in range(bank_size)]
    bank.append(DatasetQuestion(dataset_id=pyq.id, chapter_id=chapter.id, topic_id=topic.id, bloom_level=B.APPLY,
                                difficulty=D.EASY, question_type=QuestionType.SHORT_ANSWER, text="pyq q",
                                answer="a", created_by=teacher.id, external_uid=f"uid-{uuid.uuid4()}"))
    db_session.add_all(bank)
    test = VoiceTest(class_id=section.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=topic.id,
                     duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS)
    db_session.add(test)
    await db_session.flush()
    return {"teacher": teacher, "test": test, "imp": imp, "pyq": pyq, "bank": bank}


async def _build(db_session, s, total, picked=None):
    return await build_homework_questions(
        db_session, user=s["teacher"], test=s["test"], total=total, bloom_weights={B.APPLY: 1},
        difficulty=HomeworkDifficulty.EASY, fallback_dataset_id=s["imp"].id, picked_dataset_ids=picked, seed="t")


@pytest.mark.asyncio
async def test_bank_first_then_generation_for_the_gaps(db_session, monkeypatch):
    s = await _seed(db_session, bank_size=2)
    sent = {}

    async def fake_gap_fill(payload):
        sent.update(payload)
        return {"questions": [{"slot_index": 0, "question_type": "short_answer", "text": "generated q", "options": [],
                               "answer": "g", "solution": "", "grounded_in": "11.6 Resistors"}],
                "unfilled_slot_indexes": [], "attempts": 1}

    monkeypatch.setattr(generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(generation_client, "gap_fill", fake_gap_fill)
    drafts = await _build(db_session, s, total=3, picked=[s["imp"].id])  # teacher picked Important only

    assert {d.text for d in drafts[:2]} == {"bank q0", "bank q1"}
    assert all(d.source_question_id for d in drafts[:2]) and drafts[2].text == "generated q"
    assert drafts[2].source_question_id is None and drafts[2].dataset_id == s["imp"].id
    assert "pyq q" not in [d.text for d in drafts]                      # not a picked dataset
    assert sent["sections"][0]["text"].startswith("In series") and len(sent["slots"]) == 1
    assert {"bank q0", "bank q1"} <= set(sent["avoid"])


@pytest.mark.asyncio
async def test_generation_down_falls_back_so_the_homework_is_never_short(db_session, monkeypatch):
    s = await _seed(db_session, bank_size=1)

    async def down(payload):
        raise generation_client.GenerationServiceUnavailableError("down")

    monkeypatch.setattr(generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(generation_client, "gap_fill", down)
    drafts = await _build(db_session, s, total=3, picked=[s["imp"].id])
    assert len(drafts) == 3
    assert sum(d.source_question_id is not None for d in drafts) == 1  # the one bank question; rest legacy


@pytest.mark.asyncio
async def test_generation_down_prefers_any_unused_bank_question_on_the_topic(db_session, monkeypatch):
    s = await _seed(db_session, bank_size=1)
    extra = DatasetQuestion(dataset_id=s["imp"].id, chapter_id=s["test"].chapter_id, topic_id=s["test"].topic_id,
                            bloom_level=B.REMEMBER, difficulty=D.HARD, question_type=QuestionType.SHORT_ANSWER,
                            text="off-bloom bank q", answer="a", created_by=s["teacher"].id, external_uid="x-1")
    db_session.add(extra)
    await db_session.flush()
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)
    drafts = await _build(db_session, s, total=2, picked=[s["imp"].id])   # both slots APPLY/EASY
    assert [d.text for d in drafts] == ["bank q0", "off-bloom bank q"]    # real question over a placeholder


async def _hand_out(db_session, s, drafts, status):
    hw = Homework(test_id=s["test"].id, class_id=s["test"].class_id, gap_topic="", gap_mastery_percent=0,
                  total_questions=2, difficulty=HomeworkDifficulty.EASY,
                  target_mode=AssignmentTargetMode.WHOLE_CLASS, status=status)
    db_session.add(hw)
    await db_session.flush()
    db_session.add_all([HomeworkQuestion(homework_id=hw.id, order=i, bloom_level=d.bloom_level, dataset_id=d.dataset_id,
                                         source_question_id=d.source_question_id, text=d.text, answer=d.answer)
                        for i, d in enumerate(drafts, 1)])
    await db_session.flush()


def _bank_ids(drafts):
    return {d.source_question_id for d in drafts if d.source_question_id}


@pytest.mark.asyncio
async def test_questions_the_class_already_had_are_not_reused(db_session, monkeypatch):
    s = await _seed(db_session, bank_size=2)
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)
    first = await _build(db_session, s, total=2, picked=[s["imp"].id])
    await _hand_out(db_session, s, first, HomeworkStatus.ASSIGNED)

    second = await _build(db_session, s, total=2, picked=[s["imp"].id])
    assert not _bank_ids(first) & _bank_ids(second)


@pytest.mark.asyncio
async def test_a_draft_still_in_review_doesnt_use_up_the_pool(db_session, monkeypatch):
    s = await _seed(db_session, bank_size=2)
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)
    first = await _build(db_session, s, total=2, picked=[s["imp"].id])
    await _hand_out(db_session, s, first, HomeworkStatus.REVIEW)

    second = await _build(db_session, s, total=2, picked=[s["imp"].id])
    assert _bank_ids(first) == _bank_ids(second)
