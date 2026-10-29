"""T-044: who_is_right problems stored before the fix can have an explanation that
names the wrong friend (the answer was swapped at storage, the explanation text was not).
Quiz tests and practice read these rows by id, so repairing the row repairs them.

- Zip <-> Bolt swapped in the explanation (spec and the explanation column) when that
  makes it agree with the marked answer;
- otherwise the row becomes kind 'who_is_right_retired' (never picked again).
Rows that already agree are untouched, so it is safe to re-run. Without --go it only
lists the ids. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/repair_whoisright_explanations.py [--go]
"""
import asyncio
import sys

sys.path.insert(0, "/app")


async def main(args: list[str]) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from src.config.settings import get_settings
    from src.domain.models import PracticeQuestion
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services import question_types as qt

    init_engine(get_settings().database_url)
    async with get_session() as session:
        rows = (await session.execute(select(PracticeQuestion).where(PracticeQuestion.kind == "who_is_right"))).scalars().all()
        fixed = retired = 0
        for q in rows:
            spec = dict(q.spec or {})
            if "correct" not in spec or not qt.names_wrong_friend(spec):
                continue
            swapped = {**spec, "explanation": qt.swap_cast(spec["explanation"])}
            if qt.names_wrong_friend(swapped):
                print("retire", q.id)
                q.kind, retired = "who_is_right_retired", retired + 1
            else:
                print("fix   ", q.id)
                q.spec, q.explanation, fixed = swapped, swapped["explanation"], fixed + 1
                flag_modified(q, "spec")
        print(f"{len(rows)} who_is_right problems; fix {fixed}, retire {retired}")
        if "--go" in args:
            await session.commit()
        else:
            print("dry run - add --go to apply")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
