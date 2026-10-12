"""Archives a Homework's questions into the confirming student's personal
Practice Bank - see confirm_completion's call site in
src/api/routes/student_homework.py. Not "Question Bank" (see
spec/CLAUDE.md's Terminology list and school_oversight.py's
SchoolQuestionBankEntryOut) - that's the teacher/admin-side generation-
source catalog; this is a single student's own kept-for-later set.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import Chapter, Homework, HomeworkQuestion, PracticeBankEntry, Topic, VoiceTest


async def archive_homework_questions(
    session: AsyncSession, *, homework: Homework, student_id: uuid.UUID
) -> None:
    """Idempotent on replay via PracticeBankEntry's own
    (student_id, source_question_id) uniqueness - re-running this for a
    student who already has every one of this Homework's questions
    archived is a no-op, matching confirm_completion's own "can be called
    more than once" contract. Only ever inserts new rows; never updates or
    deletes an existing archived copy, even if the source HomeworkQuestion
    is edited/replaced afterwards (see PracticeBankEntry's docstring on
    why it's a standalone copy).
    """
    test = await session.get(VoiceTest, homework.test_id) if homework.test_id else None
    if test is None and homework.topic_id:  # a topic quiz's homework: file it under its topic
        topic = await session.get(Topic, homework.topic_id)
        chapter = await session.get(Chapter, topic.chapter_id)
        test = SimpleNamespace(subject_id=chapter.subject_id, chapter_id=chapter.id, topic_id=topic.id)

    existing = await session.execute(
        select(PracticeBankEntry.source_question_id).where(
            PracticeBankEntry.student_id == student_id,
            PracticeBankEntry.source_homework_id == homework.id,
        )
    )
    already_archived = {row[0] for row in existing.all()}

    questions = await session.execute(
        select(HomeworkQuestion)
        .where(HomeworkQuestion.homework_id == homework.id)
        .order_by(HomeworkQuestion.order)
    )
    now = datetime.now(timezone.utc)
    for q in questions.scalars().all():
        if q.id in already_archived:
            continue
        session.add(
            PracticeBankEntry(
                student_id=student_id,
                source_homework_id=homework.id,
                source_question_id=q.id,
                subject_id=test.subject_id if test else None,
                chapter_id=test.chapter_id if test else None,
                topic_id=test.topic_id if test else None,
                bloom_level=q.bloom_level,
                order=q.order,
                text=q.text,
                answer=q.answer,
                archived_at=now,
            )
        )
