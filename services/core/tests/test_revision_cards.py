"""Revision cards: one deck per topic generated once and reused for every
student, 'generating' for whoever loses the claim, failed retry after 10
minutes, teacher/admin regenerate, access checks and the chapter list.

Real rows against the dev Postgres via db_session; the generation service is
stubbed - no model calls."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from shared.errors import NotFoundError
from src.api.deps import get_current_app_user
from src.api.routes import revision_cards as routes
from src.domain.models import (
    Chapter,
    RevisionDeck,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    StudentProfile,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
)
from src.services import revision_cards as service

CARDS = [{"kind": "concept", "title": "Refraction", "body": "Light bends at a boundary.", "answer": None}] * 5 + [
    {"kind": "check", "title": "Quick check", "body": "Which way does light bend in glass?",
     "answer": "Towards the normal."}]


def _user(role, school_id, name="U"):
    return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=name,
                role=role, school_id=school_id, status=UserStatus.ACTIVE)


async def _seed(db):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=10)
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    light = Chapter(subject_id=subject.id, name="Light", order_index=1, grade=10)
    teacher, student, other_teacher, admin = (_user(Role.TEACHER, school.id), _user(Role.STUDENT, school.id),
                                              _user(Role.TEACHER, school.id), _user(Role.SCHOOL_ADMIN, school.id))
    db.add_all([section, light, teacher, student, other_teacher, admin])
    await db.flush()
    refraction = Topic(chapter_id=light.id, name="REFRACTION OF LIGHT", text_content="Light bends when ...")
    mirrors = Topic(chapter_id=light.id, name="Mirrors", text_content="A plane mirror ...")
    db.add_all([refraction, mirrors, SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True),
                TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                StudentProfile(user_id=student.id, class_id=section.id, roll_number=1)])
    await db.flush()
    return school, subject, light, refraction, mirrors, teacher, student, other_teacher, admin


@pytest.fixture
def calls(monkeypatch):
    made = []
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)

    async def revision_cards(payload):
        made.append((payload, service.ai_context.current("x")["Feature"]))
        return {"cards": CARDS}

    monkeypatch.setattr(service.generation_client, "revision_cards", revision_cards)
    return made


async def _open(db, topic, user):
    tasks = BackgroundTasks()
    out = await routes.get_topic_revision_cards(str(topic.id), tasks, user=user, session=db)
    return out, len(tasks.tasks)


async def test_one_generation_then_reused_by_every_student(db_session, calls):
    *_, light, refraction, _, teacher, student, _, _ = await _seed(db_session)
    first, scheduled = await _open(db_session, refraction, student)
    assert first.status == "generating" and first.cards == [] and scheduled == 1
    # a second student opening it meanwhile waits for the same generation
    second, scheduled = await _open(db_session, refraction, teacher)
    assert second.status == "generating" and scheduled == 0

    await service.generate(db_session, refraction.id)
    assert len(calls) == 1
    payload, feature = calls[0]
    assert feature == "revision_cards" and "Light bends when" in payload["context"]
    assert payload["topic"] == "REFRACTION OF LIGHT" and payload["chapter"] == "Light" and payload["grade"] == 10

    later, scheduled = await _open(db_session, refraction, student)
    assert later.status == "ready" and scheduled == 0 and len(calls) == 1
    assert later.model_dump(by_alias=True)["topicName"] == "REFRACTION OF LIGHT"
    assert later.cards[-1].answer == "Towards the normal." and later.cards[0].answer is None


async def test_a_failed_deck_is_retried_only_after_10_minutes(db_session, monkeypatch):
    *_, refraction, _, _, student, _, _ = await _seed(db_session)
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: False)
    await _open(db_session, refraction, student)
    await service.generate(db_session, refraction.id)
    out, scheduled = await _open(db_session, refraction, student)
    assert out.status == "failed" and scheduled == 0

    async def age(minutes):
        await db_session.execute(update(RevisionDeck).where(RevisionDeck.topic_id == refraction.id).values(
            updated_at=func.now() - timedelta(minutes=minutes)))

    await age(5)
    assert (await _open(db_session, refraction, student))[1] == 0
    await age(11)
    out, scheduled = await _open(db_session, refraction, student)
    assert out.status == "generating" and scheduled == 1


async def test_regenerate_is_for_the_subjects_teacher_and_admins(db_session, calls, monkeypatch):
    *_, refraction, _, teacher, student, other_teacher, admin = await _seed(db_session)
    await _open(db_session, refraction, student)
    await service.generate(db_session, refraction.id)

    with pytest.raises(NotFoundError):  # teaches another subject
        await routes.regenerate_topic_revision_cards(str(refraction.id), BackgroundTasks(), user=other_teacher,
                                                     session=db_session)
    for who in (teacher, admin):
        tasks = BackgroundTasks()
        out = await routes.regenerate_topic_revision_cards(str(refraction.id), tasks, user=who, session=db_session)
        assert out.status == "generating" and len(tasks.tasks) == 1
        deck = await service.generate(db_session, refraction.id)
        assert deck.status == "ready" and deck.generated_by == who.id
    assert len(calls) == 3

    # a regenerate that fails keeps the cards it was replacing
    await routes.regenerate_topic_revision_cards(str(refraction.id), BackgroundTasks(), user=admin, session=db_session)
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: False)
    deck = await service.generate(db_session, refraction.id)
    assert deck.status == "ready" and len(deck.cards) == 6


def test_students_cannot_regenerate(_core_env):
    from src.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_app_user] = lambda: _user(Role.STUDENT, uuid.uuid4())
    response = TestClient(app).post(f"/api/v1/topics/{uuid.uuid4()}/revision-cards/regenerate",
                                    headers={"Authorization": "Bearer x"})
    assert response.status_code == 403


async def test_access_checks(db_session, calls):
    school, subject, light, refraction, *_ = await _seed(db_session)
    other_school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    db_session.add(other_school)
    await db_session.flush()
    outsider, grade9 = _user(Role.STUDENT, other_school.id), _user(Role.STUDENT, school.id)
    nine = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
    db_session.add_all([outsider, grade9, nine])
    await db_session.flush()
    db_session.add(StudentProfile(user_id=grade9.id, class_id=nine.id, roll_number=2))
    await db_session.flush()
    for who in (outsider, grade9):
        with pytest.raises(NotFoundError):
            await _open(db_session, refraction, who)
        with pytest.raises(NotFoundError):
            await routes.get_chapter_revision_cards(str(light.id), user=who, session=db_session)
    with pytest.raises(NotFoundError):
        await _open(db_session, Topic(id=uuid.uuid4()), grade9)


async def test_chapter_list_shows_status_without_generating(db_session, calls):
    *_, light, refraction, mirrors, _, student, _, _ = await _seed(db_session)
    await _open(db_session, refraction, student)
    await service.generate(db_session, refraction.id)
    out = await routes.get_chapter_revision_cards(str(light.id), user=student, session=db_session)
    assert out.chapter_name == "Light"
    assert [(t.topic_name, t.status, t.card_count) for t in out.topics] == [
        ("Mirrors", None, 0), ("REFRACTION OF LIGHT", "ready", 6)]
    assert out.model_dump(by_alias=True)["topics"][0].keys() == {"topicId", "topicName", "status", "cardCount"}
    assert len(calls) == 1 and await service.deck_of(db_session, mirrors.id) is None


def test_clean_cards_keeps_only_well_formed_ones():
    assert service.clean_cards([
        {"kind": "concept", "title": " T ", "body": " B ", "answer": "dropped"},
        {"kind": "essay", "title": "T", "body": "B"}, {"kind": "check", "title": "T", "body": ""},
        {"kind": "check", "title": "Q", "body": "Why?", "answer": " Because. "}]) == [
        {"kind": "concept", "title": "T", "body": "B", "answer": None},
        {"kind": "check", "title": "Q", "body": "Why?", "answer": "Because."}]


async def test_concurrent_first_opens_claim_once(db_session):
    """Two real connections racing to claim the same topic: exactly one wins."""
    url = db_session.bind.engine.url
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            subject = Subject(name=f"Race-{uuid.uuid4()}")
            setup.add(subject)
            await setup.flush()
            chapter = Chapter(subject_id=subject.id, name="Race")
            setup.add(chapter)
            await setup.flush()
            topic = Topic(chapter_id=chapter.id, name="Race topic")
            setup.add(topic)
            await setup.commit()

        async def claim():
            async with AsyncSession(engine) as s:
                won = await service.claim(s, topic)
                await asyncio.sleep(0.05)  # hold the row until both have tried
                await s.commit()
                return won

        assert sorted(await asyncio.gather(claim(), claim(), claim())) == [False, False, True]
    finally:
        async with AsyncSession(engine) as cleanup:
            await cleanup.execute(delete(RevisionDeck).where(RevisionDeck.topic_id == topic.id))
            await cleanup.execute(delete(Topic).where(Topic.id == topic.id))
            await cleanup.execute(delete(Chapter).where(Chapter.id == chapter.id))
            await cleanup.execute(delete(Subject).where(Subject.id == subject.id))
            await cleanup.commit()
        await engine.dispose()
