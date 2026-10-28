"""Daily practice's spoken-answer pool (practice_questions, kind 'voice'), C-002:
raw bank questions that fail services/speakable.py's check (fill in the
blanks, True/False, "write the equation", LaTeX, several parts) were imported
before the check existed.

- a never-reworded row that fails speakable.problem -> kind 'voice_retired'
  (never picked again; past practice items keep their own kind and history);
- a reworded row whose wording fails speakable.spoken_problem -> its rubric is
  cleared, so it is reworded and re-checked the next time it is picked
  (and an MCQ is served if that fails).
Rows that pass are untouched, so it is safe to re-run. Without --go it only
counts them. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/retire_unspeakable_practice_voice.py [--go]
"""
import asyncio
import sys
from collections import Counter

sys.path.insert(0, "/app")


async def main(args: list[str]) -> None:
    from sqlalchemy import select

    from src.config.settings import get_settings
    from src.domain.models import DatasetQuestion, PracticeQuestion
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services import speakable

    go = "--go" in args
    init_engine(get_settings().database_url)
    async with get_session() as session:
        rows = (await session.execute(
            select(PracticeQuestion, DatasetQuestion.question_type)
            .outerjoin(DatasetQuestion, DatasetQuestion.id == PracticeQuestion.source_question_id)
            .where(PracticeQuestion.kind == "voice"))).all()
        retired, reset = Counter(), Counter()
        for q, question_type in rows:
            if not q.expected_points:
                if reason := speakable.problem(q.text, question_type):
                    retired[reason] += 1
                    q.kind = "voice_retired"
            elif reason := speakable.spoken_problem(q.text):
                reset[reason] += 1
                q.expected_points, q.misconceptions = [], []
        print(f"{len(rows)} spoken practice questions; retire {sum(retired.values())} {dict(retired)}; "
              f"re-word {sum(reset.values())} {dict(reset)}")
        if go:
            await session.commit()
        else:
            print("dry run - add --go to apply")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
