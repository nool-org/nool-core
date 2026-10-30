"""Push triggers (services/push.notify): voice test scheduled, homework
assigned / auto-created, MCQ test unlocked. A fake send_push records the
calls; a failing one must never break the request."""

import asyncio
import contextlib
import types
import uuid

import pytest
from fastapi import BackgroundTasks

from src.api.routes import homework, voice_test
from src.api.schemas.homework import AssignHomeworkIn
from src.api.schemas.roster import AssignmentTarget
from src.api.schemas.voice_test import CreateTestIn
from src.domain.models import (
    AssignmentTargetMode, BloomLevel, HomeworkStatus, SchoolCurriculum, TeacherClassAssignment
)
from src.services import homework_autogen, mcq_test, push
from tests.test_homework_assign import _seed_homework
from tests.test_homework_assign import _seed_school as _hw_school
from tests.test_homework_assign import _seed_student as _hw_student
from tests.test_homework_assign import _seed_teacher as _hw_teacher
from tests.test_mcq_test import _seed as _mcq_seed
from tests.test_mcq_test import schema  # noqa: F401 - fixture
from tests.test_voice_test import (_questions_ready, _seed_class_subject_chapter_topic, _seed_school, _seed_student,
                                   _seed_teacher)


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake(session, user_ids, title, body, data=None, kind=None):
        calls.append({"user_ids": {str(u) for u in user_ids}, "title": title, "data": data})
        return len(calls)

    monkeypatch.setattr(push, "send_push", fake)
    return calls


@pytest.fixture
def broken(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("expo exploded")

    monkeypatch.setattr(push, "send_push", boom)


async def _scheduled_test(db):
    school = await _seed_school(db)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db, school.id)
    teacher = await _seed_teacher(db, school.id)
    db.add(TeacherClassAssignment(teacher_id=teacher.id, class_id=school_class.id, subject_id=subject.id))
    db.add(SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True))
    a = await _seed_student(db, school.id, school_class.id, roll_number=1)
    b = await _seed_student(db, school.id, school_class.id, roll_number=2)
    body = CreateTestIn(
        class_id=str(school_class.id), subject_id=str(subject.id), chapter_id=str(chapter.id),
        topic_id=str(topic.id), bloom_levels=[BloomLevel.UNDERSTAND], duration_minutes=20,
        completion_window_hours=48, target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS),
    )
    created = await voice_test.create_test(body, background_tasks=BackgroundTasks(), user=teacher, session=db)
    await _questions_ready(db, created.id)
    result = await voice_test.schedule_test(created.id, user=teacher, _feature=teacher, session=db)
    return result, {str(a.id), str(b.id)}


async def test_scheduling_a_voice_test_pushes_each_targeted_student(db_session, sent):
    result, students = await _scheduled_test(db_session)
    assert sent == [{"user_ids": students, "title": "New voice test: Chapter 1",
                     "data": {"type": "voice_test", "testId": result.id}}]


async def test_a_push_failure_never_breaks_scheduling(db_session, broken):
    result, _ = await _scheduled_test(db_session)
    assert result.status == "SCHEDULED"


async def test_a_test_scheduled_for_later_opens_then_and_pushes_nothing_now(db_session, sent, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from src.api.schemas.voice_test import ScheduleTestIn

    real = voice_test.schedule_test
    opens = datetime.now(timezone.utc) + timedelta(days=1)

    async def later(test_id, user, _feature, session):
        return await real(test_id, body=ScheduleTestIn(opens_at=opens), user=user, _feature=_feature, session=session)

    monkeypatch.setattr(voice_test, "schedule_test", later)
    result, _ = await _scheduled_test(db_session)
    assert result.status == "SCHEDULED"
    assert abs((result.opens_at - opens).total_seconds()) < 1
    assert abs((result.due_at - (opens + timedelta(hours=48))).total_seconds()) < 1
    assert sent == []


async def _assign(db):
    school = await _hw_school(db)
    teacher = await _hw_teacher(db, school.id)
    hw, school_class = await _seed_homework(db, school.id, teacher)
    student = await _hw_student(db, school.id, school_class.id)
    out = await homework.assign_homework(
        homework_id=str(hw.id), user=teacher, session=db,
        body=AssignHomeworkIn(target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS),
                              completion_window_hours=48))
    return hw, student, out


async def test_assigning_homework_pushes_the_students(db_session, sent):
    hw, student, _ = await _assign(db_session)
    assert sent == [{"user_ids": {str(student.id)}, "title": "Homework ready: Field direction",
                     "data": {"type": "homework", "homeworkId": str(hw.id)}}]


async def test_a_push_failure_never_breaks_assigning(db_session, broken):
    hw, _, out = await _assign(db_session)
    assert out.id == str(hw.id) and hw.status == HomeworkStatus.ASSIGNED


async def test_auto_homework_pushes_its_student(db_session, sent, monkeypatch):
    student_id, hw = uuid.uuid4(), types.SimpleNamespace(id=uuid.uuid4(), gap_topic="Electricity")

    class Session:  # the real session, but `get` finds "the test"
        def __getattr__(self, name):
            return getattr(db_session, name)

        async def get(self, *_):
            return object()

    @contextlib.asynccontextmanager
    async def fake_get_session():
        yield Session()

    async def build(*_a, **_k):
        return hw

    monkeypatch.setattr("src.repositories.get_session", fake_get_session)
    monkeypatch.setattr(homework_autogen, "auto_generate_homework", build)
    homework_autogen.schedule_auto_homework(test_id=uuid.uuid4(), student_id=student_id, mastery_percent=40,
                                            bloom_levels=[BloomLevel.REMEMBER])
    await asyncio.gather(*list(homework_autogen._background))
    assert sent == [{"user_ids": {str(student_id)}, "title": "Homework ready: Electricity",
                     "data": {"type": "homework", "homeworkId": str(hw.id)}}]


async def test_mcq_unlock_pushes_once(schema, sent):  # noqa: F811
    s = await _mcq_seed(schema, taken_hours_ago=49)
    [mcq] = await mcq_test.sync_for_student(schema, s["student"].id)
    assert mcq.unlocked_at is not None
    await mcq_test.sync_for_student(schema, s["student"].id)  # already unlocked - no second push
    assert sent == [{"user_ids": {str(s["student"].id)}, "title": "Your pick-one round is ready",
                     "data": {"type": "mcq_test", "mcqTestId": str(mcq.id)}}]


async def test_locked_mcq_does_not_push(schema, sent):  # noqa: F811
    s = await _mcq_seed(schema, taken_hours_ago=1)
    await mcq_test.sync_for_student(schema, s["student"].id)
    assert sent == []


async def test_a_push_failure_never_breaks_the_mcq_unlock(schema, broken):  # noqa: F811
    s = await _mcq_seed(schema, taken_hours_ago=49)
    [mcq] = await mcq_test.sync_for_student(schema, s["student"].id)
    assert mcq_test.status(mcq) == "available"


def test_push_switch_lives_in_core_settings(monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.delenv("EXPO_PUSH_ENABLED", raising=False)
    assert get_settings().expo_push_enabled is False and not push.push_enabled()
    monkeypatch.setenv("EXPO_PUSH_ENABLED", "true")
    assert push.push_enabled()
