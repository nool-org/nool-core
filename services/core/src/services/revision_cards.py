"""Revision cards: a topic's 6-8 short cards (key ideas, definitions,
formulas, a common mistake, quick self-checks), written once by the
generation service (POST /v1/revision/cards, gpt-5-mini) and shown to every
student - routes/revision_cards.py. Never regenerated except by a
teacher/admin's explicit regenerate.

Exactly one generation per topic, even when many students open it at once:
the caller that inserts the topic's 'generating' row (ON CONFLICT DO NOTHING
on the unique topic_id) generates; everyone else sees 'generating' and the
app polls. A 'failed' deck - or a 'generating' one whose worker died - is
claimed again by the next request after RETRY_AFTER.

Grounded in the topic's own textbook text (homework_builder._sections, as MCQ
and voice test prep are); a topic without synced text falls back to kg's
textbook context for it.

pregenerate_for_topic(topic_id) builds a deck ahead of the first open - meant
to be scheduled when a teacher marks a topic taught (PUT
/classes/{id}/topics/{id}/taught in routes/daily_practice.py, after its
commit: background_tasks.add_task(revision_cards.pregenerate_for_topic, topic.id)).
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import Chapter, RevisionDeck, Subject, Topic
from src.repositories import get_session
from src.services import ai_context, generation_client, kg_client
from src.services.homework_builder import _sections

logger = logging.getLogger(__name__)

RETRY_AFTER = timedelta(minutes=10)
MODEL = "gpt-5-mini"  # the generation service's VERIFY_MODEL - see services/generation/src/services/revision_cards.py
KINDS = ("concept", "definition", "formula", "mistake", "check")


async def deck_of(session: AsyncSession, topic_id) -> RevisionDeck | None:
    return (await session.execute(select(RevisionDeck).where(RevisionDeck.topic_id == topic_id)
                                  .execution_options(populate_existing=True))).scalar_one_or_none()


async def claim(session: AsyncSession, topic: Topic, *, force: bool = False,
                user_id: uuid.UUID | None = None) -> bool:
    """Marks the topic's deck 'generating' if this caller should generate it -
    no deck yet, a failed/stuck one past RETRY_AFTER, or (force) any deck not
    being generated right now. True for exactly one of concurrent callers.
    The caller commits, then runs generate()."""
    won = (await session.execute(pg_insert(RevisionDeck).values(
        id=uuid.uuid4(), topic_id=topic.id, chapter_id=topic.chapter_id, status="generating", cards=[],
        generated_by=user_id,
    ).on_conflict_do_nothing(index_elements=["topic_id"]).returning(RevisionDeck.id))).first()
    if won:
        return True
    stale = RevisionDeck.updated_at < func.now() - RETRY_AFTER
    retryable = (or_(RevisionDeck.status != "generating", stale) if force
                 else or_(RevisionDeck.status == "failed", RevisionDeck.status == "generating") & stale)
    # the row lock makes a concurrent claimer re-check updated_at and lose
    claimed = await session.execute(update(RevisionDeck).where(RevisionDeck.topic_id == topic.id, retryable).values(
        status="generating", updated_at=func.now(), generated_by=user_id).returning(RevisionDeck.id))
    return claimed.first() is not None


async def open_deck(session: AsyncSession, topic: Topic) -> tuple[RevisionDeck, bool]:
    """The topic's deck, and whether this caller just claimed its generation
    (commit, then generate). A ready deck is returned as-is - no model call."""
    deck = await deck_of(session, topic.id)
    claimed = False
    if deck is None or deck.status != "ready":
        claimed = await claim(session, topic)
        deck = await deck_of(session, topic.id)
    return deck, claimed


def clean_cards(cards) -> list[dict]:
    """Only well-formed cards, in the stored shape."""
    out = []
    for c in cards or []:
        if c.get("kind") in KINDS and str(c.get("title") or "").strip() and str(c.get("body") or "").strip():
            answer = str(c.get("answer") or "").strip() if c["kind"] == "check" else ""
            out.append({"kind": c["kind"], "title": str(c["title"]).strip(), "body": str(c["body"]).strip(),
                        "answer": answer or None})
    return out


async def _context(session: AsyncSession, topic: Topic, chapter: Chapter, subject: Subject | None,
                   grade: int) -> str:
    _, sections = await _sections(session, topic, topic.id)
    if sections:
        return "\n\n".join(f"## {s['title']}\n{s['text']}" for s in sections)
    if chapter.order_index is None or subject is None:
        return ""  # never synced from kg - nothing to ground in
    try:
        result = await kg_client.generate_assessor_content(
            subject=subject.kg_subject_name or subject.name, board="CBSE", grade=grade,
            chapters=[{"number": chapter.order_index, "name": chapter.name}], topic_names=[topic.name],
            bloom_levels=["REMEMBER"], num_questions=1)
    except Exception:
        logger.warning("revision_cards_kg_context_failed", extra={"topic_id": str(topic.id)})
        return ""
    return result.get("textbook_context") or ""


async def generate(session: AsyncSession, topic_id) -> RevisionDeck | None:
    """Writes the claimed deck: 'ready' with its cards, or 'failed' (retried
    by the next request after RETRY_AFTER). Commits."""
    topic = await session.get(Topic, topic_id)
    deck = await deck_of(session, topic_id)
    if topic is None or deck is None:
        return None
    chapter = await session.get(Chapter, topic.chapter_id)
    subject = await session.get(Subject, chapter.subject_id)
    grade = (subject.grade if subject else None) or chapter.grade or 10
    cards = []
    context = await _context(session, topic, chapter, subject, grade)
    await session.commit()  # no transaction held open during the (slow) model call
    if context and generation_client.is_configured():
        try:
            with ai_context.feature("revision_cards"):
                result = await generation_client.revision_cards({
                    "grade": grade, "subject": subject.name if subject else "Science", "chapter": chapter.name,
                    "topic": topic.name, "context": context})
            cards = clean_cards(result.get("cards"))
        except generation_client.GenerationServiceUnavailableError:
            pass
    if not cards:
        logger.warning("revision_cards_failed", extra={"topic_id": str(topic_id), "grounded": bool(context)})
    # a failed regenerate keeps the deck it was replacing
    await session.execute(update(RevisionDeck).where(RevisionDeck.topic_id == topic_id).values(
        status="ready" if cards or deck.cards else "failed", cards=cards or deck.cards,
        model=MODEL if cards else deck.model, updated_at=func.now()))
    await session.commit()
    return await deck_of(session, topic_id)


async def generate_in_background(topic_id) -> None:
    """BackgroundTasks entrypoint - its own session; the request's is closed by now."""
    async with get_session() as session:
        try:
            await generate(session, topic_id)
        except Exception:
            logger.exception("revision_cards_generation_crashed", extra={"topic_id": str(topic_id)})
            await session.rollback()
            await session.execute(update(RevisionDeck).where(RevisionDeck.topic_id == topic_id).values(
                status=case((func.jsonb_array_length(RevisionDeck.cards) > 0, "ready"), else_="failed"),
                updated_at=func.now()))
            await session.commit()


async def pregenerate_for_topic(topic_id) -> None:
    """Builds the topic's deck if it has none yet (or a retryable failed one) -
    a no-op when it is ready or already being generated. Best effort, own session."""
    try:
        async with get_session() as session:
            topic = await session.get(Topic, topic_id)
            if topic is None:
                return
            _, claimed = await open_deck(session, topic)
            await session.commit()
        if claimed:
            await generate_in_background(topic_id)
    except Exception:
        logger.exception("revision_cards_pregenerate_failed", extra={"topic_id": str(topic_id)})
