"""Build Fix Milo's homework / Who is right? questions from a chapter's official
board questions (services/derived_questions.py). Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/derive_from_bank.py "Real Numbers"
"""
import asyncio
import sys

sys.path.insert(0, "/app")


async def main(chapter_name: str, subject: str = "Mathematics") -> None:
    from sqlalchemy import select

    from src.config.settings import get_settings
    from src.domain.models import Chapter, Subject
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services import derived_questions

    init_engine(get_settings().database_url)
    async with get_session() as session:
        chapter = (await session.execute(select(Chapter).join(Subject, Subject.id == Chapter.subject_id).where(
            Chapter.name == chapter_name, Subject.name == subject))).scalar_one()
        made = await derived_questions.derive(session, chapter.id)
        await session.commit()
        print(f"{chapter_name}: made {made}")


asyncio.run(main(*sys.argv[1:]))
