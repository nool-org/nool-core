"""Revision cards - one deck per topic, generated once and shared by every
student (src/services/revision_cards.py). Opening a topic generates its deck
in the background the first time ('generating' -> the app polls); only a
teacher/admin regenerate replaces a deck."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError
from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes.curriculum import _ensure_subject_open_to
from src.api.schemas.revision_cards import (
    ChapterRevisionCardsOut,
    ChapterRevisionTopicOut,
    RevisionCardOut,
    TopicRevisionCardsOut,
)
from src.domain.models import (
    Feature,
    Chapter,
    RevisionDeck,
    Role,
    SchoolClass,
    SchoolCurriculum,
    StudentProfile,
    Subject,
    Topic,
    User,
)
from src.services import revision_cards

router = APIRouter(prefix="/api/v1", tags=["revision-cards"])


async def _get(session: AsyncSession, model, id_: str, label: str):
    try:
        row = await session.get(model, uuid.UUID(id_))
    except ValueError:
        row = None
    if row is None:
        raise NotFoundError(f'{label} "{id_}" was not found.')
    return row


async def _ensure_chapter_visible(session: AsyncSession, user: User, chapter: Chapter) -> None:
    """Teachers/school admins: as the curriculum tree (subject enabled for the
    school; a teacher's own subject). Students: subject enabled for their
    school, and the subject/chapter (when graded) is their class's grade.
    Super admins: any."""
    if user.role == Role.SUPER_ADMIN:
        return
    if user.role != Role.STUDENT:
        await _ensure_subject_open_to(session, user, chapter.subject_id)
        return
    subject = await session.get(Subject, chapter.subject_id)
    grade = (await session.execute(select(SchoolClass.grade).join(
        StudentProfile, StudentProfile.class_id == SchoolClass.id).where(
        StudentProfile.user_id == user.id))).scalar_one_or_none()
    enabled = (await session.execute(select(SchoolCurriculum.id).where(
        SchoolCurriculum.school_id == user.school_id, SchoolCurriculum.subject_id == chapter.subject_id,
        SchoolCurriculum.enabled.is_(True)).limit(1))).first()
    if not enabled or subject is None or any(g not in (None, grade) for g in (subject.grade, chapter.grade)):
        raise NotFoundError("That chapter isn't available to you.")


def _deck_out(topic: Topic, chapter: Chapter, deck: RevisionDeck) -> TopicRevisionCardsOut:
    return TopicRevisionCardsOut(
        topic_id=str(topic.id), topic_name=topic.name, chapter_name=chapter.name, status=deck.status,
        cards=[RevisionCardOut(**c) for c in deck.cards] if deck.status == "ready" else [])


@router.get("/topics/{topic_id}/revision-cards")
async def get_topic_revision_cards(
    topic_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.STUDENT, Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.REVISION_CARDS)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicRevisionCardsOut:
    """The topic's cards when ready; otherwise starts the (one) generation and
    answers 'generating' - poll until 'ready' or 'failed'."""
    topic = await _get(session, Topic, topic_id, "Topic")
    chapter = await session.get(Chapter, topic.chapter_id)
    await _ensure_chapter_visible(session, user, chapter)
    deck, claimed = await revision_cards.open_deck(session, topic)
    await session.commit()
    if claimed:
        background_tasks.add_task(revision_cards.generate_in_background, topic.id)
    return _deck_out(topic, chapter, deck)


@router.get("/chapters/{chapter_id}/revision-cards")
async def get_chapter_revision_cards(
    chapter_id: str,
    user: User = Depends(require_role(Role.STUDENT, Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.REVISION_CARDS)),
    session: AsyncSession = Depends(get_db_session),
) -> ChapterRevisionCardsOut:
    """The chapter's topics with their deck status - generates nothing."""
    chapter = await _get(session, Chapter, chapter_id, "Chapter")
    await _ensure_chapter_visible(session, user, chapter)
    rows = (await session.execute(
        select(Topic, RevisionDeck).outerjoin(RevisionDeck, RevisionDeck.topic_id == Topic.id)
        .where(Topic.chapter_id == chapter.id).order_by(Topic.name))).all()
    return ChapterRevisionCardsOut(chapter_name=chapter.name, topics=[
        ChapterRevisionTopicOut(topic_id=str(t.id), topic_name=t.name, status=d.status if d else None,
                                card_count=len(d.cards) if d and d.status == "ready" else 0)
        for t, d in rows])


@router.post("/topics/{topic_id}/revision-cards/regenerate")
async def regenerate_topic_revision_cards(
    topic_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN, Role.SUPER_ADMIN)),
    _feature: User = Depends(require_feature(Feature.REVISION_CARDS)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicRevisionCardsOut:
    """The only way to replace a deck: a teacher of the subject or an admin.
    'generating' until the new cards are written; a failed regenerate keeps
    the old cards (back to 'ready'). A no-op while a generation is running."""
    topic = await _get(session, Topic, topic_id, "Topic")
    chapter = await session.get(Chapter, topic.chapter_id)
    await _ensure_chapter_visible(session, user, chapter)
    claimed = await revision_cards.claim(session, topic, force=True, user_id=user.id)
    await session.commit()
    if claimed:
        background_tasks.add_task(revision_cards.generate_in_background, topic.id)
    return _deck_out(topic, chapter, await revision_cards.deck_of(session, topic.id))
