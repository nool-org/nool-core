"""C-013: bank question external_uid 'cbse-2025-30-4-1-q26(B)' (HCF/LCM of x^2y^3, xy^4, x^5y^2) is a
Real Numbers question tagged Polynomials. Moves it (dataset_questions) and its copies in
practice_questions to the Real Numbers chapter of the same subject (same grade), clears the topic tag,
and removes its copies from Polynomials homework that no student answered. Matched by external_uid, so
re-running changes nothing. Without --go it only counts. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/retag_c013.py [--go]
"""
import asyncio
import sys

sys.path.insert(0, "/app")

EXTERNAL_UID = "cbse-2025-30-4-1-q26(B)"


async def main(args: list[str]) -> None:
    from sqlalchemy import delete, func, select, update

    from src.config.settings import get_settings
    from src.domain.models import (Chapter, DatasetQuestion, Homework, HomeworkAnswer, HomeworkQuestion,
                                   PracticeQuestion, Topic)
    from src.repositories import get_session
    from src.repositories.database import init_engine

    go = "--go" in args
    init_engine(get_settings().database_url)
    async with get_session() as session:
        rows = (await session.execute(select(DatasetQuestion).where(
            DatasetQuestion.external_uid == EXTERNAL_UID))).scalars().all()
        if not rows:
            print("no such bank question")
            return
        moved = 0
        for q in rows:
            wrong = await session.get(Chapter, q.chapter_id)
            target = (await session.execute(select(Chapter).where(
                Chapter.subject_id == wrong.subject_id, Chapter.grade.is_not_distinct_from(wrong.grade),
                func.lower(Chapter.name).like("%real numbers%")))).scalars().first()
            if target is None:
                print(f"{q.id}: no Real Numbers chapter in the subject, skipped")
                continue
            if wrong.id == target.id:
                continue  # already moved
            moved += 1
            print(f"{q.id}: {wrong.name} -> {target.name}")
            q.chapter_id, q.topic_id, q.subtopic_id = target.id, None, None
            await session.execute(update(PracticeQuestion).where(PracticeQuestion.source_question_id == q.id)
                                  .values(chapter_id=target.id, topic_id=None))
            poly_topics = select(Topic.id).where(Topic.chapter_id == wrong.id)
            copies = select(HomeworkQuestion.id).join(Homework, Homework.id == HomeworkQuestion.homework_id).where(
                HomeworkQuestion.source_question_id == q.id,
                (Homework.chapter_id == wrong.id) | Homework.topic_id.in_(poly_topics),
                HomeworkQuestion.id.not_in(select(HomeworkAnswer.homework_question_id)))
            ids = (await session.execute(copies)).scalars().all()
            print(f"  homework copies to remove (unanswered, in {wrong.name} homework): {len(ids)}")
            if ids:
                await session.execute(delete(HomeworkQuestion).where(HomeworkQuestion.id.in_(ids)))
        print(f"{moved} bank question(s) to move")
        if go:
            await session.commit()
        else:
            print("dry run - add --go to apply")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
