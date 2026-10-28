"""POST /homework/{id}/generate is claimed under a row lock: a second call gets
409 instead of doubling the questions, and a retry after FAILED replaces the
questions a previous attempt left behind."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from shared.errors import ConflictError
from src.api.routes import homework
from src.domain.models import (
    AssignmentTargetMode, BloomLevel, Dataset, Homework, HomeworkDifficulty, HomeworkQuestion, HomeworkStatus,
)
from src.services.homework_builder import QuestionDraft
from tests.test_tenant_isolation import _school


async def _setup(db_session, monkeypatch, status):
    a = await _school(db_session)
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=a["subject"].id, grade=10)
    hw = Homework(test_id=a["test"].id, class_id=a["section"].id, gap_topic="Electricity", gap_mastery_percent=50,
                  total_questions=2, difficulty=HomeworkDifficulty.EASY, target_mode=AssignmentTargetMode.WHOLE_CLASS,
                  status=status)
    db_session.add_all([dataset, hw])
    await db_session.flush()

    async def grounding(*_a, **_kw):
        return dataset.id

    async def build(*_a, total, **_kw):
        return [QuestionDraft(bloom_level=BloomLevel.REMEMBER, text=f"Q{i}", answer="A", dataset_id=dataset.id)
                for i in range(total)]

    monkeypatch.setattr(homework, "grounding_dataset_id", grounding)
    monkeypatch.setattr(homework, "build_homework_questions", build)
    monkeypatch.setattr(homework.homework_autogen, "build_homework_questions", build)  # generate goes through it
    return a, hw, dataset


async def _texts(db_session, hw):
    rows = await db_session.execute(select(HomeworkQuestion.text).where(HomeworkQuestion.homework_id == hw.id))
    return sorted(t for (t,) in rows.all())


@pytest.mark.asyncio
async def test_a_second_generate_is_refused_and_adds_nothing(db_session, monkeypatch):
    a, hw, _ = await _setup(db_session, monkeypatch, HomeworkStatus.GENERATING)
    await homework.generate_homework(str(hw.id), user=a["teacher"], session=db_session)
    assert hw.status == HomeworkStatus.REVIEW and hw.generation_started_at is None

    with pytest.raises(ConflictError):
        await homework.generate_homework(str(hw.id), user=a["teacher"], session=db_session)
    assert await _texts(db_session, hw) == ["Q0", "Q1"]


@pytest.mark.asyncio
async def test_a_generation_in_progress_blocks_a_parallel_call(db_session, monkeypatch):
    a, hw, _ = await _setup(db_session, monkeypatch, HomeworkStatus.GENERATING)
    hw.generation_started_at = datetime.now(timezone.utc)
    await db_session.flush()
    with pytest.raises(ConflictError):
        await homework.generate_homework(str(hw.id), user=a["teacher"], session=db_session)


@pytest.mark.asyncio
async def test_a_retry_after_failure_replaces_leftovers_even_with_a_stale_claim(db_session, monkeypatch):
    a, hw, dataset = await _setup(db_session, monkeypatch, HomeworkStatus.FAILED)
    hw.generation_started_at = datetime.now(timezone.utc) - timedelta(minutes=homework.GENERATION_LOCK_MINUTES + 1)
    db_session.add(HomeworkQuestion(homework_id=hw.id, order=1, bloom_level=BloomLevel.REMEMBER,
                                    dataset_id=dataset.id, text=f"old-{uuid.uuid4()}", answer="x"))
    await db_session.flush()

    await homework.generate_homework(str(hw.id), user=a["teacher"], session=db_session)
    assert await _texts(db_session, hw) == ["Q0", "Q1"]


async def _in_review(db_session, monkeypatch):
    a, hw, dataset = await _setup(db_session, monkeypatch, HomeworkStatus.GENERATING)
    await homework.generate_homework(str(hw.id), user=a["teacher"], session=db_session)
    questions = (await homework.list_homework_questions(str(hw.id), user=a["teacher"], session=db_session)).items
    return a, hw, questions


@pytest.mark.asyncio
async def test_review_edits_text_and_answer_and_delete_renumbers(db_session, monkeypatch):
    from src.api.schemas.homework import UpdateHomeworkQuestionIn

    a, hw, (q0, q1) = await _in_review(db_session, monkeypatch)
    out = await homework.update_homework_question(str(hw.id), q0.id, UpdateHomeworkQuestionIn(answer="B"),
                                                  user=a["teacher"], session=db_session)
    assert (out.items[0].text, out.items[0].answer) == ("Q0", "B")

    out = await homework.delete_homework_question(str(hw.id), q0.id, user=a["teacher"], session=db_session)
    assert [(q.id, q.order) for q in out.items] == [(q1.id, 1)] and hw.total_questions == 1
    with pytest.raises(ConflictError):   # never an empty homework
        await homework.delete_homework_question(str(hw.id), q1.id, user=a["teacher"], session=db_session)

    hw.status = HomeworkStatus.ASSIGNED   # students may be answering: no more edits
    with pytest.raises(ConflictError):
        await homework.update_homework_question(str(hw.id), q1.id, UpdateHomeworkQuestionIn(text="x"),
                                                user=a["teacher"], session=db_session)


@pytest.mark.asyncio
async def test_replace_brings_its_own_answer_and_avoids_the_rest_of_the_homework(db_session, monkeypatch):
    a, hw, (q0, _) = await _in_review(db_session, monkeypatch)
    seen = {}

    async def build(*_a, keep, **kw):
        seen["keep"] = sorted(d.text for d in keep)
        return [QuestionDraft(bloom_level=BloomLevel.REMEMBER, text="fresh", answer="fresh answer",
                              dataset_id=keep[0].dataset_id)]

    monkeypatch.setattr(homework, "build_homework_questions", build)
    out = await homework.replace_homework_question(str(hw.id), q0.id, user=a["teacher"], session=db_session)
    assert (out.items[0].text, out.items[0].answer) == ("fresh", "fresh answer")
    assert seen["keep"] == ["Q0", "Q1"]
