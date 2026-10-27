"""The live screen's actions on a scheduled Speak with Milo test: remind the
students who haven't finished (a push) and give everyone 24 more hours."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from shared.errors import ConflictError, NotFoundError, ValidationError
from src.api.deps import get_current_app_user
from src.api.routes import voice_test
from src.api.schemas.voice_test import RemindIn
from src.domain.models import Role, StudentTestResult
from src.services import push
from tests.test_authorization import _fake_user
from tests.test_push_triggers import _scheduled_test
from tests.test_voice_test import _seed_school, _seed_teacher


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake(session, user_ids, title, body, data=None):
        calls.append({"user_ids": {str(u) for u in user_ids}, "title": title})
        return 1

    monkeypatch.setattr(push, "send_push", fake)
    return calls


async def _live(db):
    result, students = await _scheduled_test(db)
    test = await db.get(voice_test.VoiceTest, uuid.UUID(result.id))
    teacher = await db.get(voice_test.User, (await db.execute(
        voice_test.select(voice_test.TeacherClassAssignment.teacher_id).where(
            voice_test.TeacherClassAssignment.class_id == test.class_id))).scalar_one())
    return test, teacher, sorted(students)


async def test_remind_pushes_only_students_who_have_not_finished(db_session, sent):
    test, teacher, (a, b) = await _live(db_session)
    sent.clear()  # the scheduling push
    db_session.add(StudentTestResult(test_id=test.id, student_id=uuid.UUID(a), mastery_percent=70))
    await db_session.flush()

    out = await voice_test.remind_students(str(test.id), RemindIn(), user=teacher, _feature=teacher, session=db_session)
    assert out.reminded == 1 and sent[0]["user_ids"] == {b}

    sent.clear()
    out = await voice_test.remind_students(str(test.id), RemindIn(student_ids=[b, str(uuid.uuid4()), "nope"]),
                                           user=teacher, _feature=teacher, session=db_session)
    assert out.reminded == 1 and sent[0]["user_ids"] == {b}  # strangers dropped

    with pytest.raises(ValidationError):
        await voice_test.remind_students(str(test.id), RemindIn(student_ids=[a]),
                                         user=teacher, _feature=teacher, session=db_session)


async def test_remind_and_extend_are_only_for_the_tests_own_teacher(db_session, sent):
    test, _, _ = await _live(db_session)
    stranger = await _seed_teacher(db_session, (await _seed_school(db_session)).id)
    with pytest.raises(NotFoundError):
        await voice_test.remind_students(str(test.id), RemindIn(), user=stranger, _feature=stranger, session=db_session)
    with pytest.raises(NotFoundError):
        await voice_test.extend_test(str(test.id), user=stranger, _feature=stranger, session=db_session)


async def test_extend_adds_24_hours_and_reopens_a_closed_test(db_session, sent):
    test, teacher, _ = await _live(db_session)
    test.scheduled_at = datetime.now(timezone.utc) - timedelta(hours=49)  # 48 h window: closed
    await db_session.flush()
    with pytest.raises(ConflictError, match="closed"):
        await voice_test.remind_students(str(test.id), RemindIn(), user=teacher, _feature=teacher, session=db_session)

    out = await voice_test.extend_test(str(test.id), user=teacher, _feature=teacher, session=db_session)
    assert out.completion_window_hours == 72
    assert out.due_at > datetime.now(timezone.utc)

    test.completion_window_hours = 14 * 24
    await db_session.flush()
    with pytest.raises(ValidationError):
        await voice_test.extend_test(str(test.id), user=teacher, _feature=teacher, session=db_session)


def test_students_cannot_remind_or_extend(_core_env):
    from src.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_app_user] = lambda: _fake_user(Role.STUDENT)
    client = TestClient(app)
    for path in ("remind", "extend"):
        response = client.post(f"/api/v1/tests/{uuid.uuid4()}/{path}", json={}, headers={"Authorization": "Bearer x"})
        assert response.status_code == 403
