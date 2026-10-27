"""Homework a teacher sets straight from a chapter (the Create sheet's
Homework, no Speak with Milo before it): created, listed, generated,
assigned, and on the student's task list - only for the chapter's teacher."""

import uuid

import pytest

from shared.errors import ForbiddenError, NotFoundError, ValidationError
from src.api.routes import homework
from src.api.schemas.bloom import BloomTarget
from src.api.schemas.homework import AssignHomeworkIn, CreateHomeworkIn
from src.api.schemas.roster import AssignmentTarget
from src.domain.models import AssignmentTargetMode, BloomLevel, Chapter, HomeworkDifficulty, HomeworkStatus
from src.services import student_tasks
from tests.test_tenant_isolation import _school


def _body(**kw):
    return CreateHomeworkIn(dataset_ids=[], total_questions=2, difficulty=HomeworkDifficulty.EASY,
                            bloom_distribution=[BloomTarget(level=BloomLevel.REMEMBER, value=2)], **kw)


async def _chapter(db, a):
    return (await db.execute(homework.select(Chapter).where(Chapter.subject_id == a["subject"].id))).scalar_one()


async def test_a_teacher_sets_homework_on_a_chapter_and_the_class_gets_it(db_session, monkeypatch):
    a = await _school(db_session)
    chapter = await _chapter(db_session, a)
    out = await homework.create_homework(_body(class_id=str(a["section"].id), chapter_id=str(chapter.id)),
                                         user=a["teacher"], session=db_session)
    assert (out.test_id, out.chapter_id, out.chapter_name, out.status) == (
        None, str(chapter.id), "Electricity", HomeworkStatus.GENERATING)
    assert out.id in {h.id for h in (await homework.list_homework(user=a["teacher"], session=db_session)).items}

    built = {}

    async def build(*_a, test, total, fallback_dataset_id, **_kw):
        built["chapter"] = test.chapter_id
        return [homework.QuestionDraft(bloom_level=BloomLevel.REMEMBER, text=f"Q{i}", answer="A",
                                       dataset_id=fallback_dataset_id) for i in range(total)]

    monkeypatch.setattr(homework, "build_homework_questions", build)
    from src.domain.models import Dataset
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=a["subject"].id, grade=10)
    db_session.add(dataset)
    await db_session.flush()

    async def grounding_real(*_a, test, **_kw):
        built["subject"] = test.subject_id
        return dataset.id

    monkeypatch.setattr(homework, "grounding_dataset_id", grounding_real)
    out = await homework.generate_homework(out.id, user=a["teacher"], session=db_session)
    assert out.status == HomeworkStatus.REVIEW
    assert (built["subject"], built["chapter"]) == (a["subject"].id, chapter.id)  # grounded in the chapter

    await homework.assign_homework(out.id, AssignHomeworkIn(
        target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS), completion_window_hours=48),
        user=a["teacher"], session=db_session)
    tasks = await student_tasks._chapter_homework_tasks(db_session, a["student"], homework.datetime.now(
        homework.timezone.utc))
    [task] = tasks
    assert (task["kind"], task["title"], task["steps"][0]["target_id"]) == ("homework", "Electricity", out.id)


async def test_only_the_chapters_teacher_and_a_complete_request(db_session):
    a, b = await _school(db_session), await _school(db_session)
    chapter = await _chapter(db_session, a)
    with pytest.raises(ValidationError):
        await homework.create_homework(_body(class_id=str(a["section"].id)), user=a["teacher"], session=db_session)
    with pytest.raises(NotFoundError):  # another school's class
        await homework.create_homework(_body(class_id=str(a["section"].id), chapter_id=str(chapter.id)),
                                       user=b["teacher"], session=db_session)
    other_chapter = await _chapter(db_session, b)  # a subject a's teacher doesn't teach
    with pytest.raises(ForbiddenError):
        await homework.create_homework(_body(class_id=str(a["section"].id), chapter_id=str(other_chapter.id)),
                                       user=a["teacher"], session=db_session)
    with pytest.raises(NotFoundError):
        await homework.create_homework(_body(class_id=str(a["section"].id), chapter_id="nope"),
                                       user=a["teacher"], session=db_session)
