"""Teacher assistant + dashboard summary: AI answers grounded in the
teacher's own facts, the keyword fallback when the model is down, and the
15-minute summary cache."""

import uuid

from src.api.routes import assistant
from src.api.schemas.assistant import SendMessageIn
from src.domain.models import Role, School, User, UserStatus
from src.services import generation_client


async def _teacher(db) -> User:
    school = School(name=f"S-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    db.add(school)
    await db.flush()
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="Sudha",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    db.add(teacher)
    await db.flush()
    return teacher


async def test_reply_comes_from_the_model_with_the_teachers_facts_and_history(db_session, monkeypatch):
    teacher = await _teacher(db_session)
    seen = {}

    async def fake_reply(facts, history, message):
        seen.update(facts=facts, history=history, message=message)
        return "No tests yet - create one from Home."

    monkeypatch.setattr(generation_client, "assistant_reply", fake_reply)
    out = await assistant.send_message(SendMessageIn(text="How is my class doing?"), user=teacher, session=db_session)

    assert out.text == "No tests yet - create one from Home."
    assert seen["facts"]["teacher"] == "Sudha" and seen["facts"]["recent_voice_tests"] == []
    assert seen["history"][0]["role"] == "assistant"  # the greeting, oldest first
    assert seen["message"] == "How is my class doing?"


async def test_falls_back_to_how_to_answers_when_the_model_is_down(db_session, monkeypatch):
    teacher = await _teacher(db_session)

    async def down(*_):
        raise generation_client.GenerationServiceUnavailableError("down")

    monkeypatch.setattr(generation_client, "assistant_reply", down)
    out = await assistant.send_message(SendMessageIn(text="how do I make a voice test"), user=teacher, session=db_session)
    assert out.text.startswith("To create a Voice Test")


async def test_summary_is_cached_and_empty_when_the_model_is_down(db_session, monkeypatch):
    teacher = await _teacher(db_session)
    calls = []

    async def fake_summary(facts):
        calls.append(facts)
        return ["Nothing needs you today."]

    monkeypatch.setattr(generation_client, "teacher_summary", fake_summary)
    first = await assistant.teacher_summary(user=teacher, session=db_session)
    second = await assistant.teacher_summary(user=teacher, session=db_session)
    assert first.bullets == second.bullets == ["Nothing needs you today."] and len(calls) == 1
    await assistant.teacher_summary(refresh=True, user=teacher, session=db_session)
    assert len(calls) == 2

    async def down(_):
        raise generation_client.GenerationServiceUnavailableError("down")

    monkeypatch.setattr(generation_client, "teacher_summary", down)
    other = await _teacher(db_session)
    assert (await assistant.teacher_summary(user=other, session=db_session)).bullets == []
