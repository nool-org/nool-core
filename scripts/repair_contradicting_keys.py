"""T-062: stored paper answer keys that name a different option than the answer stored with the MCQ
(and MCQs with two identical options). Contradicting keys are deleted, so the teacher regenerates them
with the new guard; duplicate-option questions are only listed (a human re-picks them). Without --go it
only reports. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/repair_contradicting_keys.py [--go]
"""
import asyncio
import sys

sys.path.insert(0, "/app")


async def main(args: list[str]) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import undefer_group

    from src.config.settings import get_settings
    from src.domain.models import QuestionPaperQuestion
    from src.domain.models.paper_answer_key import QuestionPaperAnswerKey
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services.paper_actions import COPILOT_COLUMNS
    from src.services.paper_answer_key import key_contradicts

    go = "--go" in args
    init_engine(get_settings().database_url)
    async with get_session() as session:
        rows = (await session.execute(select(QuestionPaperQuestion).options(undefer_group(COPILOT_COLUMNS))
                                      .where(QuestionPaperQuestion.options.is_not(None)))).scalars().all()
        bad = 0
        for q in rows:
            opts = [str(o).strip().lower() for o in q.options or []]
            if len(set(opts)) != len(opts):
                print(f"duplicate options: paper {q.paper_id} q {q.id}: {q.text[:80]!r}")
            key = await session.get(QuestionPaperAnswerKey, q.id)
            if key and key_contradicts(q.answer, key.answer, q.options):
                bad += 1
                print(f"contradicting key: paper {q.paper_id} q {q.id}: stored {q.answer!r} / key {key.answer[:60]!r}")
                if go:
                    await session.delete(key)
        print(f"{bad} contradicting key(s)")
        if go:
            await session.commit()
        else:
            print("dry run - add --go to apply")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
