"""Deleting a subject/chapter/topic takes its curriculum subtree with it, and
is refused (409, nothing deleted) while anything else still uses it."""

import uuid

import pytest

from shared.errors import ConflictError
from src.api.routes import admin_catalog
from src.domain.models import Chapter, Role, Subject, Subtopic, Topic, User, UserStatus
from tests.test_tenant_isolation import _school


async def _admin(db_session):
    admin = User(firebase_uid=f"sa-{uuid.uuid4()}", email=f"{uuid.uuid4()}@x.com", display_name="SA",
                 role=Role.SUPER_ADMIN, status=UserStatus.ACTIVE)
    db_session.add(admin)
    await db_session.flush()
    return admin


@pytest.mark.asyncio
async def test_a_subject_goes_with_its_chapters_topics_and_subtopics(db_session):
    admin = await _admin(db_session)
    subject = Subject(name=f"Maths-{uuid.uuid4()}", grade=9)
    db_session.add(subject)
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Polynomials", grade=9)
    db_session.add(chapter)
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="Zeroes")
    db_session.add(topic)
    await db_session.flush()
    subtopic = Subtopic(topic_id=topic.id, name="Remainder theorem")
    db_session.add(subtopic)
    await db_session.flush()
    ids = [(Subject, subject.id), (Chapter, chapter.id), (Topic, topic.id), (Subtopic, subtopic.id)]

    await admin_catalog.delete_subject(str(subject.id), actor=admin, session=db_session)
    db_session.expunge_all()
    for model, id_ in ids:
        assert await db_session.get(model, id_) is None


@pytest.mark.asyncio
async def test_a_chapter_a_test_uses_is_refused_and_kept(db_session):
    admin = await _admin(db_session)
    a = await _school(db_session)
    with pytest.raises(ConflictError, match="voice tests"):
        await admin_catalog.delete_chapter(str(a["test"].chapter_id), actor=admin, session=db_session)
    db_session.expunge_all()
    assert await db_session.get(Chapter, a["test"].chapter_id) is not None
