"""Re-prepare stored Speak with Milo questions that fail today's "can this be
answered aloud" check (services/speakable.py, C-001): several asks in one,
an equation said aloud, too long. Prepared before those rules, nothing
re-checked them, and _reused copied them into new tests.

For each failing question (Fix Milo cards are skipped):
- its test has no session yet -> reworded again (voice_test_prep.reword_question,
  which keeps a new wording only when it passes the check);
- otherwise, or when rewording fails -> needs_rewording, so it is never
  copied into another test and the teacher sees it flagged.
Rows that pass are untouched, so it is safe to re-run. Without --go it only
lists them. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/reprep_unspeakable_voice_questions.py [--go]
"""
import asyncio
import sys

sys.path.insert(0, "/app")


async def main(args: list[str]) -> None:
    from sqlalchemy import select

    from src.config.settings import get_settings
    from src.domain.models import AiAssessorSession, VoiceTest, VoiceTestQuestion
    from src.repositories import get_session
    from src.repositories.database import init_engine
    from src.services import speakable, voice_test_prep

    go = "--go" in args
    init_engine(get_settings().database_url)
    async with get_session() as session:
        rows = (await session.execute(select(VoiceTestQuestion).where(VoiceTestQuestion.card.is_(None)))).scalars().all()
        bad = [(q, r) for q in rows if (r := speakable.spoken_problem(q.spoken_text, q.hints))]
        started = set((await session.execute(select(AiAssessorSession.test_id).where(
            AiAssessorSession.test_id.in_({q.voice_test_id for q, _ in bad})))).scalars().all()) if bad else set()
        print(f"{len(bad)} of {len(rows)} spoken questions fail the check")
        for q, reason in bad:
            print(f"  {q.id} test={q.voice_test_id} {reason}: {q.spoken_text!r}")
            if not go:
                continue
            test = await session.get(VoiceTest, q.voice_test_id)
            if q.voice_test_id not in started and await voice_test_prep.reword_question(session, test, q):
                print(f"    reworded: {q.spoken_text!r}")
            else:
                q.needs_rewording = True
                print("    flagged needs_rewording")
            await session.commit()
    if not go:
        print("dry run - add --go to fix them")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
