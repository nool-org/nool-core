"""Fix Milo's homework and Who is right? questions BUILT FROM official board
questions (dataset questions with a marking-scheme solution), so the maths
in them is CBSE's - not invented:

- fix_steps from a worked solution (short/long/case answers): Milo's steps are
  the official ones with one changed to a typical mistake; the right fix is
  the official line.
- who_is_right from an Assertion-Reason question or a "can it / is it ...?
  give reasons" question: the claim and the real reason are the official ones.

Kept in the shared practice pool as origin "bank" with source_question_id,
so quizzes and tests prefer them; a source is converted once per kind.
Run per chapter (scripts/derive_from_bank.py) - a few rupees a chapter.
"""

import logging
import re
import uuid
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    BloomLevel, Chapter, DatasetQuestion, PracticeQuestion, QuestionType, SchoolDataset, Subject, Topic,
)
from src.services import ai_context, answer_checks, daily_practice, generation_client, question_types
from src.services.homework_builder import _sections

logger = logging.getLogger(__name__)

LEVEL = {"fix_steps": BloomLevel.EVALUATE, "who_is_right": BloomLevel.ANALYZE}
BATCH = 6  # slots per generation call
_REASONED = re.compile(r"\b(can|is|does|will|are)\b.*\?.*\b(reason|justify|explain)", re.IGNORECASE | re.DOTALL)


def kind_for(q: DatasetQuestion) -> str | None:
    """Which question type an official question can become, if any."""
    if q.format == "assertion-reason":
        return "who_is_right"
    if q.question_type in (QuestionType.SHORT_ANSWER, QuestionType.LONG_ANSWER) and _REASONED.search(q.text):
        return "who_is_right"
    if q.question_type in (QuestionType.SHORT_ANSWER, QuestionType.LONG_ANSWER) and len(q.solution or "") >= 40:
        return "fix_steps"
    return None


async def derive(session: AsyncSession, chapter_id: uuid.UUID, grade: int = 10, limit: int = 40) -> dict:
    """Converts up to `limit` of the chapter's not-yet-converted official
    questions; returns {kind: made}. Needs the questions' topics (their
    textbook text grounds the call). Caller commits."""
    chapter = await session.get(Chapter, chapter_id)
    subject = await session.get(Subject, chapter.subject_id)
    done = {(sid, kind) for sid, kind in (await session.execute(select(
        PracticeQuestion.source_question_id, PracticeQuestion.kind).where(
        PracticeQuestion.chapter_id == chapter_id, PracticeQuestion.kind.in_(tuple(LEVEL))))).all()}
    enabled = select(SchoolDataset.dataset_id).where(SchoolDataset.enabled.is_(True))  # never a switched-off bank
    candidates = [(q, k) for q in (await session.execute(select(DatasetQuestion).where(
        DatasetQuestion.chapter_id == chapter_id, DatasetQuestion.topic_id.is_not(None),
        DatasetQuestion.dataset_id.in_(enabled))
        .order_by(DatasetQuestion.id))).scalars().all() if (k := kind_for(q)) and (q.id, k) not in done][:limit]
    made = {"fix_steps": 0, "who_is_right": 0}
    for topic_id in dict.fromkeys(q.topic_id for q, _ in candidates):
        topic = await session.get(Topic, topic_id)
        _, sections = await _sections(session, SimpleNamespace(chapter_id=chapter_id), topic_id)
        mine = [(q, k) for q, k in candidates if q.topic_id == topic_id]
        for start in range(0, len(mine), BATCH):
            batch = mine[start:start + BATCH]
            payload = {
                "grade": grade, "subject": subject.name, "chapter": chapter.name, "topic": daily_practice.tidy(topic.name),
                "context": "\n\n".join(f"{s['title']}\n{s['text']}" for s in sections) or topic.name,
                "slots": [{"kind": k, "bloom_level": LEVEL[k].value, "source": {
                    "question": q.text, "answer": q.answer or "", "solution": q.solution or "",
                    "options": q.options}} for q, k in batch],
                "checks": [{"kind": k, "describe": c.describe} for k, c in answer_checks.KINDS.items()],
                "avoid": [],
            }
            try:
                with ai_context.feature("derived_questions"):
                    body = await generation_client.special_questions(payload)
            except generation_client.GenerationServiceUnavailableError:
                logger.warning("derived_questions_unavailable", extra={"topic_id": str(topic_id)})
                continue
            for g in body.get("questions", []):
                n = g.get("slot_index", -1)
                if not 0 <= n < len(batch) or g.get("kind") != batch[n][1]:
                    continue
                source, kind = batch[n]
                try:
                    question_types.validate(kind, g.get("spec") or {})
                except question_types.SpecError as exc:
                    logger.warning("derived_question_rejected", extra={"kind": kind, "reason": str(exc)})
                    continue
                spec = question_types.shuffle(kind, g["spec"], str(source.id))
                session.add(PracticeQuestion(
                    chapter_id=chapter_id, topic_id=topic_id, bloom_level=LEVEL[kind], kind=kind, text=g["text"],
                    spec=spec, options=None, correct_index=None, explanation=g["spec"].get("explanation") or None,
                    expected_points=[], misconceptions=[], source_question_id=source.id, origin="bank", used_count=0))
                made[kind] += 1
            await session.flush()
    return made
