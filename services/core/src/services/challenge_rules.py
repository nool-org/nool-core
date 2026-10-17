"""One challenge cycle per chapter per class and format: a class gets at most
one Speak with Milo and one Fix Milo's homework on a chapter - challenge ->
homework -> re-check - not a pile of them. Both on one chapter is fine (the
suggester proposes it when the chapter has equations to balance and ideas to
explain). Drafts and failed preparations
don't count (there's no delete for a draft, so it mustn't block); anything
opened to the class does. Checked when one is set and again when it opens.
A plan with extra_challenges_per_month (Pro) lets a class take that many more
a month on chapters it already has one on."""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError
from src.domain.models import QuizTest, SchoolClass, VoiceTest
from src.domain.models.voice_test import TestStatus
from src.repositories.subscription_repository import get_active_plan

OPEN_QUIZ_STATUSES = ("scheduled", "published")


async def taken_formats(session: AsyncSession, class_id, chapter_id, *, ignore_id=None) -> set[str]:
    """The formats ("voice", "fix_steps") the class already has a challenge (not a draft) in on this chapter."""
    voice = select(VoiceTest.id).where(
        VoiceTest.class_id == class_id, VoiceTest.chapter_id == chapter_id, VoiceTest.status != TestStatus.DRAFT)
    written = select(QuizTest.id).where(
        QuizTest.class_id == class_id, QuizTest.chapter_id == chapter_id, QuizTest.status.in_(OPEN_QUIZ_STATUSES))
    if ignore_id is not None:
        ignore = uuid.UUID(str(ignore_id))
        voice, written = voice.where(VoiceTest.id != ignore), written.where(QuizTest.id != ignore)
    return {f for f, q in (("voice", voice), ("fix_steps", written)) if (await session.execute(q.limit(1))).first()}


async def extras_used_this_month(session: AsyncSession, class_id, *, ignore_id=None) -> int:
    """This month's (IST) challenges in the class on a chapter + format that already had an earlier one."""
    voice = select(VoiceTest.id, VoiceTest.chapter_id, VoiceTest.created_at).where(
        VoiceTest.class_id == class_id, VoiceTest.status != TestStatus.DRAFT)
    written = select(QuizTest.id, QuizTest.chapter_id, QuizTest.created_at).where(
        QuizTest.class_id == class_id, QuizTest.status.in_(OPEN_QUIZ_STATUSES))
    month_start = datetime.now(ZoneInfo("Asia/Kolkata")).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = 0
    for fmt, q in (("voice", voice), ("fix_steps", written)):
        firsts: set = set()
        for row_id, chapter_id, created in sorted((await session.execute(q)).all(), key=lambda r: r[2]):
            if ignore_id is not None and str(row_id) == str(ignore_id):
                continue
            if chapter_id in firsts and created >= month_start:
                used += 1
            firsts.add(chapter_id)
    return used


async def ensure_one_per_chapter(session: AsyncSession, class_id, chapter_id, fmt: str, *, ignore_id=None) -> None:
    if fmt not in await taken_formats(session, class_id, chapter_id, ignore_id=ignore_id):
        return
    label = "Speak with Milo" if fmt == "voice" else "Fix Milo's homework"
    school_class = await session.get(SchoolClass, uuid.UUID(str(class_id)))
    plan = await get_active_plan(session, school_class.school_id) if school_class else None
    extra = plan.extra_challenges_per_month if plan else 0
    if extra and await extras_used_this_month(session, class_id, ignore_id=ignore_id) < extra:
        return
    if extra:
        raise ConflictError(f"This class already has a {label} on this chapter, and this month's extra "
                            f"challenge{'s' if extra > 1 else ''} on your plan {'are' if extra > 1 else 'is'} used. "
                            "Open it from Challenges to see how the class is doing.")
    raise ConflictError(f"This class already has a {label} on this chapter - one per chapter. "
                        "Open it from Challenges to see how the class is doing.")
