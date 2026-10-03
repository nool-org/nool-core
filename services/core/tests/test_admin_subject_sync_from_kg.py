"""POST /admin/subjects/{id}/sync-from-kg and GET .../kg-tree - the sync a
Super Admin actually uses from the Curriculum page's per-Subject "Sync"
action. No Dataset involved at all, unlike the older
POST /admin/curriculum/sync-from-kg (see test_admin_curriculum_sync_from_kg.py).
"""

import uuid

import pytest
from sqlalchemy import select

from shared.errors import NotFoundError, ValidationError

from src.api.routes import admin_catalog
from src.api.schemas.admin_catalog import SyncSubjectFromKgIn
from src.domain.models import Chapter, Role, Subject, SubjectGrade, Subtopic, Topic, User, UserStatus


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


async def _seed_subject(db_session, *, grades: list[int]) -> Subject:
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=grades[0] if len(grades) == 1 else None)
    db_session.add(subject)
    await db_session.flush()
    for grade in grades:
        db_session.add(SubjectGrade(subject_id=subject.id, grade=grade))
    await db_session.flush()
    return subject


def _fake_tree_for(subject_name: str, ref_prefix: str, *, with_subtopic: bool = False):
    async def fake_get_curriculum_tree(*, board, grade, subject):
        if subject != subject_name:
            return {"subject": subject, "chapters": []}
        topic = {"ref": f"{ref_prefix}-T01", "number": "1.1", "name": "Topic One"}
        if with_subtopic:
            topic["subtopics"] = [{"ref": f"{ref_prefix}-S01", "number": "1.1.1", "name": "Subtopic One"}]
        return {
            "subject": subject,
            "chapters": [
                {"ref": f"{ref_prefix}-CH01", "number": 1, "name": "Chapter One", "topics": [topic]},
            ],
        }

    return fake_get_curriculum_tree


@pytest.mark.asyncio
async def test_sync_subject_from_kg_upserts_chapters_and_topics(db_session, monkeypatch):
    actor = await _seed_super_admin(db_session)
    subject = await _seed_subject(db_session, grades=[10])
    ref_prefix = str(uuid.uuid4())[:8]

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree", _fake_tree_for(subject.name, ref_prefix)
    )

    result = await admin_catalog.sync_subject_from_kg(
        str(subject.id), SyncSubjectFromKgIn(grade=10), actor=actor, session=db_session
    )

    assert result.chapters_synced == 1
    assert result.topics_synced == 1

    chapter = (
        await db_session.execute(select(Chapter).where(Chapter.kg_ref == f"{ref_prefix}-CH01"))
    ).scalar_one()
    assert chapter.subject_id == subject.id
    assert chapter.grade == 10
    topic = (await db_session.execute(select(Topic).where(Topic.kg_ref == f"{ref_prefix}-T01"))).scalar_one()
    assert topic.chapter_id == chapter.id


@pytest.mark.asyncio
async def test_sync_subject_from_kg_upserts_subtopics(db_session, monkeypatch):
    actor = await _seed_super_admin(db_session)
    subject = await _seed_subject(db_session, grades=[10])
    ref_prefix = str(uuid.uuid4())[:8]

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree",
        _fake_tree_for(subject.name, ref_prefix, with_subtopic=True),
    )

    result = await admin_catalog.sync_subject_from_kg(
        str(subject.id), SyncSubjectFromKgIn(grade=10), actor=actor, session=db_session
    )

    assert result.subtopics_synced == 1
    topic = (await db_session.execute(select(Topic).where(Topic.kg_ref == f"{ref_prefix}-T01"))).scalar_one()
    subtopic = (
        await db_session.execute(select(Subtopic).where(Subtopic.kg_ref == f"{ref_prefix}-S01"))
    ).scalar_one()
    assert subtopic.topic_id == topic.id

    # Re-running is idempotent - no duplicate row for the same kg_ref.
    await admin_catalog.sync_subject_from_kg(
        str(subject.id), SyncSubjectFromKgIn(grade=10), actor=actor, session=db_session
    )
    rows = await db_session.execute(select(Subtopic).where(Subtopic.kg_ref == f"{ref_prefix}-S01"))
    assert len(rows.scalars().all()) == 1


@pytest.mark.asyncio
async def test_sync_subject_from_kg_rejects_a_grade_not_linked_to_the_subject(db_session, monkeypatch):
    actor = await _seed_super_admin(db_session)
    subject = await _seed_subject(db_session, grades=[10])

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree", _fake_tree_for(subject.name, "x")
    )

    with pytest.raises(ValidationError):
        await admin_catalog.sync_subject_from_kg(
            str(subject.id), SyncSubjectFromKgIn(grade=9), actor=actor, session=db_session
        )


@pytest.mark.asyncio
async def test_sync_subject_from_kg_unknown_subject_404s(db_session):
    actor = await _seed_super_admin(db_session)
    with pytest.raises(NotFoundError):
        await admin_catalog.sync_subject_from_kg(
            str(uuid.uuid4()), SyncSubjectFromKgIn(grade=10), actor=actor, session=db_session
        )


@pytest.mark.asyncio
async def test_get_subject_kg_tree_returns_the_kg_preview(db_session, monkeypatch):
    actor = await _seed_super_admin(db_session)
    subject = await _seed_subject(db_session, grades=[10])
    ref_prefix = str(uuid.uuid4())[:8]

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree", _fake_tree_for(subject.name, ref_prefix)
    )

    tree = await admin_catalog.get_subject_kg_tree(
        str(subject.id), grade=10, kg_subject_name=None, _=actor, session=db_session
    )

    assert tree.grade == 10
    assert tree.chapters[0].ref == f"{ref_prefix}-CH01"
    assert tree.chapters[0].topics[0].subtopics == []


@pytest.mark.asyncio
async def test_get_subject_kg_tree_rejects_a_grade_not_linked_to_the_subject(db_session, monkeypatch):
    actor = await _seed_super_admin(db_session)
    subject = await _seed_subject(db_session, grades=[10])

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree", _fake_tree_for(subject.name, "x")
    )

    with pytest.raises(ValidationError):
        await admin_catalog.get_subject_kg_tree(
            str(subject.id), grade=9, kg_subject_name=None, _=actor, session=db_session
        )


@pytest.mark.asyncio
async def test_same_subject_name_per_grade_syncs_its_own_curriculum_with_the_same_refs(db_session, monkeypatch):
    """Every grade's graph numbers from CH01: "Science" 9 and "Science" 10
    must each get their own chapter/topic rows, and one grade's graph can't
    be synced into the other grade's subject."""
    actor = await _seed_super_admin(db_session)
    name = f"Science-{uuid.uuid4()}"
    nine, ten = Subject(name=name, grade=9), Subject(name=name, grade=10)
    db_session.add_all([nine, ten])
    await db_session.flush()
    db_session.add_all([SubjectGrade(subject_id=nine.id, grade=9), SubjectGrade(subject_id=ten.id, grade=10)])
    await db_session.flush()
    monkeypatch.setattr("src.api.routes.admin_catalog.kg_client.get_curriculum_tree", _fake_tree_for(name, "SAME"))

    for subject, grade in ((nine, 9), (ten, 10), (ten, 10)):  # the last one re-syncs: idempotent
        await admin_catalog.sync_subject_from_kg(str(subject.id), SyncSubjectFromKgIn(grade=grade), actor=actor,
                                                 session=db_session)

    chapters = (await db_session.execute(select(Chapter).where(Chapter.kg_ref == "SAME-CH01",
                                                               Chapter.subject_id.in_([nine.id, ten.id])))).scalars().all()
    assert sorted((c.grade, c.subject_id == ten.id) for c in chapters) == [(9, False), (10, True)]
    topics = (await db_session.execute(select(Topic).where(Topic.chapter_id.in_([c.id for c in chapters])))).scalars().all()
    assert len(topics) == 2 and {t.kg_ref for t in topics} == {"SAME-T01"}

    with pytest.raises(ValidationError):
        await admin_catalog.sync_subject_from_kg(str(ten.id), SyncSubjectFromKgIn(grade=9), actor=actor,
                                                 session=db_session)
