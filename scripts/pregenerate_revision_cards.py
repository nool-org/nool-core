"""Make every topic's revision deck ahead of time, so no student waits for one
(services/revision_cards.py - one deck per topic, shared by every student,
never regenerated). Topics with a ready deck are skipped, so it is safe to
re-run. Without --go it only counts what it would make. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/pregenerate_revision_cards.py [--go] [Subject]
"""
import asyncio
import sys

sys.path.insert(0, "/app")

AT_ONCE = 3  # decks generated in parallel - kind to the generation service
USD_PER_DECK = 0.004  # about what one gpt-5-mini deck costs (ai_usage_events, feature revision_cards)


async def main(args: list[str]) -> None:
    from sqlalchemy import select

    from src.config.settings import get_settings
    from src.domain.models import Chapter, RevisionDeck, Subject, Topic
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services import revision_cards

    go = "--go" in args
    subject = next((a for a in args if not a.startswith("--")), None)
    init_engine(get_settings().database_url)
    async with get_session() as session:
        query = (select(Topic.id, Topic.name).join(Chapter, Chapter.id == Topic.chapter_id)
                 .join(Subject, Subject.id == Chapter.subject_id)
                 .outerjoin(RevisionDeck, RevisionDeck.topic_id == Topic.id)
                 .where((RevisionDeck.id.is_(None)) | (RevisionDeck.status != "ready"))
                 .order_by(Subject.name, Chapter.order_index, Topic.name))
        if subject:
            query = query.where(Subject.name == subject)
        todo = (await session.execute(query)).all()
    print(f"{len(todo)} topics without a ready deck - about ${len(todo) * USD_PER_DECK:.2f}")
    if not go:
        print("dry run - add --go to make them")
        return

    gate = asyncio.Semaphore(AT_ONCE)

    async def one(topic_id, name):
        async with gate:
            await revision_cards.pregenerate_for_topic(topic_id)
            print(f"  {name}", flush=True)

    await asyncio.gather(*(one(t, n) for t, n in todo))
    async with get_session() as session:
        ready = len((await session.execute(select(RevisionDeck.id).where(RevisionDeck.status == "ready"))).all())
    print(f"done - {ready} decks ready")


asyncio.run(main(sys.argv[1:]))
