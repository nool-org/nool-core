"""Repeat detection for the rules check: a paper question that the same
class has already seen - in another paper of this school/subject/grade,
a homework question or a voice-test question for the subject, in the last
LOOKBACK_DAYS. Match = equal normalized text (lowercase, punctuation and
whitespace collapsed) or a difflib ratio >= SIMILARITY.
"""

import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.question_paper import RepeatOut
from src.domain.models import (
    Chapter,
    Homework,
    HomeworkQuestion,
    QuestionPaper,
    SchoolClass,
    VoiceTest,
    VoiceTestQuestion,
)
from src.domain.models.question_paper import QuestionPaperQuestion

LOOKBACK_DAYS = 365
SIMILARITY = 0.85
# ponytail: O(paper x pool) difflib scan over the newest MAX_POOL rows per
# source; move to pg_trgm similarity() in SQL if pools grow past this.
MAX_POOL = 2000


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _similar(a: str, b: str) -> bool:
    m = SequenceMatcher(None, a, b, autojunk=False)
    return m.real_quick_ratio() >= SIMILARITY and m.quick_ratio() >= SIMILARITY and m.ratio() >= SIMILARITY


async def _pool(session: AsyncSession, paper: QuestionPaper, since: datetime) -> list[tuple[str, str, str, datetime]]:
    """(matchedIn, text, title, date), newest first per source."""
    classes = select(SchoolClass.id).where(SchoolClass.school_id == paper.school_id, SchoolClass.grade == paper.grade)
    papers = await session.execute(
        select(QuestionPaperQuestion.text, QuestionPaper.name, QuestionPaper.created_at)
        .join(QuestionPaper, QuestionPaper.id == QuestionPaperQuestion.paper_id)
        .where(QuestionPaper.school_id == paper.school_id, QuestionPaper.subject_id == paper.subject_id,
               QuestionPaper.grade == paper.grade, QuestionPaper.id != paper.id, QuestionPaper.created_at >= since)
        .order_by(QuestionPaper.created_at.desc()).limit(MAX_POOL)
    )
    homework = await session.execute(
        select(HomeworkQuestion.text, Homework.gap_topic, Homework.created_at)
        .join(Homework, Homework.id == HomeworkQuestion.homework_id)
        .join(VoiceTest, VoiceTest.id == Homework.test_id)
        .where(VoiceTest.subject_id == paper.subject_id, Homework.class_id.in_(classes), Homework.created_at >= since)
        .order_by(Homework.created_at.desc()).limit(MAX_POOL)
    )
    voice = await session.execute(
        select(VoiceTestQuestion.original_text, Chapter.name, VoiceTest.created_at)
        .join(VoiceTest, VoiceTest.id == VoiceTestQuestion.voice_test_id)
        .join(Chapter, Chapter.id == VoiceTest.chapter_id)
        .where(VoiceTest.subject_id == paper.subject_id, VoiceTest.class_id.in_(classes),
               VoiceTest.created_at >= since)
        .order_by(VoiceTest.created_at.desc()).limit(MAX_POOL)
    )
    return ([("paper", *r) for r in papers.all()] + [("homework", *r) for r in homework.all()]
            + [("voice_test", *r) for r in voice.all()])


async def find_repeats(session: AsyncSession, paper: QuestionPaper, questions) -> list[RepeatOut]:
    if not questions:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    pool = [(where, normalize(text), title, at) for where, text, title, at in await _pool(session, paper, since)]
    exact = {}
    for item in pool:
        exact.setdefault(item[1], item)
    repeats = []
    for q in questions:
        mine = normalize(q.text)
        if not mine:
            continue
        hit = exact.get(mine) or next((item for item in pool if _similar(mine, item[1])), None)
        if hit:
            repeats.append(RepeatOut(question_id=str(q.id), matched_in=hit[0], matched_title=hit[2] or "",
                                     matched_at=hit[3]))
    return repeats
