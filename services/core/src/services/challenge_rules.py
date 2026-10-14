"""One challenge cycle per chapter per class: a class gets one Speak with Milo
(Science) or one Fix Milo's homework (Maths) on a chapter - challenge ->
homework -> re-check - not a pile of them. Drafts and failed preparations
don't count (there's no delete for a draft, so it mustn't block); anything
opened to the class does. Checked when one is set and again when it opens."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError
from src.domain.models import QuizTest, VoiceTest
from src.domain.models.voice_test import TestStatus

OPEN_QUIZ_STATUSES = ("scheduled", "published")


async def ensure_one_per_chapter(session: AsyncSession, class_id, chapter_id, *, ignore_id=None) -> None:
    voice = select(VoiceTest.id).where(
        VoiceTest.class_id == class_id, VoiceTest.chapter_id == chapter_id, VoiceTest.status != TestStatus.DRAFT)
    written = select(QuizTest.id).where(
        QuizTest.class_id == class_id, QuizTest.chapter_id == chapter_id, QuizTest.status.in_(OPEN_QUIZ_STATUSES))
    if ignore_id is not None:
        ignore = uuid.UUID(str(ignore_id))
        voice, written = voice.where(VoiceTest.id != ignore), written.where(QuizTest.id != ignore)
    if (await session.execute(voice.limit(1))).first() or (await session.execute(written.limit(1))).first():
        raise ConflictError("This class already has a challenge on this chapter - one per chapter. "
                            "Open it from Challenges to see how the class is doing.")
