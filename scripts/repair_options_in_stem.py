"""C-015: stored MCQ rows whose stem repeats the options (unlabelled, reordered) or carries a
"Choose the correct option:" line. Runs services/mcq_test.strip_inline_options over
mcq_test_questions and practice_questions (mcq) and rewrites the text only where it changes,
so it is idempotent. Without --go it only counts them. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/repair_options_in_stem.py [--go]
"""
import asyncio
import sys

sys.path.insert(0, "/app")


async def main(args: list[str]) -> None:
    from sqlalchemy import select

    from src.config.settings import get_settings
    from src.domain.models import McqTestQuestion, PracticeQuestion
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services.mcq_test import strip_inline_options

    go = "--go" in args
    init_engine(get_settings().database_url)
    async with get_session() as session:
        for model, where in ((McqTestQuestion, None), (PracticeQuestion, PracticeQuestion.kind == "mcq")):
            query = select(model) if where is None else select(model).where(where)
            changed = 0
            for row in (await session.execute(query)).scalars():
                fixed = strip_inline_options(row.text, row.options)
                if fixed != row.text:
                    changed += 1
                    row.text = fixed
            print(f"{model.__tablename__}: {changed} stems to clean")
        if go:
            await session.commit()
        else:
            print("dry run - add --go to apply")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
