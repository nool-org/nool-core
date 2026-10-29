"""Recorded voice answers in daily practice: the student records a short
answer, generation transcribes it, colearner grades it (one cheap call)
against the question's expected points. The audio is never stored or
logged - only the transcript, score and feedback are kept on the item.

prepare_voice_questions gives picked voice questions their spoken wording
and rubric (generation's voice prep, the same call voice tests use) - the
daily-practice picker calls it after choosing them.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import AppError, ValidationError
from src.domain.models import Chapter, DatasetQuestion, PracticeQuestion, Subject, Topic, User
from src.services import ai_context, colearner_client, generation_client, speakable
from src.services.paper_copilot import _decode_audio  # base64 + 5 MB check, shared with voice notes

logger = logging.getLogger(__name__)

MAX_PREP_BATCH = 12  # generation's PrepRequest limit
UNGRADED_FEEDBACK = {"good": "", "add": "We couldn't check this one - your teacher will see it."}


class TranscriptionUnavailableError(AppError):
    code = "TRANSCRIPTION_UNAVAILABLE"
    status_code = 503


async def prepare_voice_questions(session: AsyncSession, questions: list[PracticeQuestion]) -> None:
    """Voice rows without expected_points get generation's spoken wording
    (into `text`) and rubric, in one batch - only a wording that passes
    speakable.spoken_problem. Best effort: on failure the rows stay as they
    are, and daily_practice._fill serves an MCQ in their place.
    Doesn't commit - the caller does."""
    rows = [q for q in questions if q.kind == "voice" and not q.expected_points]
    if not rows:
        return
    source_ids = [q.source_question_id for q in rows if q.source_question_id]
    answers = dict((await session.execute(
        select(DatasetQuestion.id, DatasetQuestion.answer).where(DatasetQuestion.id.in_(source_ids))
    )).all()) if source_ids else {}
    # generation needs an answer to write the rubric from - a row with none is left as is
    rows = [q for q in rows if (answers.get(q.source_question_id) or q.explanation or "").strip()]
    # ponytail: one batch of 12 max; the rest get theirs next time they're picked
    rows = rows[:MAX_PREP_BATCH]
    if not rows:
        return
    chapters = {c.id: c for c in (await session.execute(
        select(Chapter).where(Chapter.id.in_({q.chapter_id for q in rows})))).scalars().all()}
    first = chapters.get(rows[0].chapter_id)
    subject = await session.get(Subject, first.subject_id) if first else None
    grade = (first.grade if first else None) or (subject.grade if subject else None) or 10
    payload = {
        "grade": grade, "subject": subject.name if subject else "Science",
        "chapter": "; ".join(dict.fromkeys(c.name for c in chapters.values()))[:300] or "General",
        "levels": [q.bloom_level.value for q in rows],
        "candidates": [{
            "id": str(q.id), "text": q.text, "answer": (answers.get(q.source_question_id) or q.explanation).strip(),
            "bloom_level": q.bloom_level.value, "topic_id": str(q.topic_id) if q.topic_id else None,
        } for q in rows],
    }
    try:
        with ai_context.feature("practice_voice_prep"):
            out = await generation_client.prep_voice_test(
                payload, generation_client.PRACTICE_VOICE_PREP_TIMEOUT_SECONDS)
        by_id = {str(p.get("dataset_question_id")): p for p in out.get("questions") or [] if isinstance(p, dict)}
    except Exception as exc:  # unavailable, or an unusable response
        logger.warning("practice_voice_prep_failed", extra={"reason": str(exc), "questions": len(rows)})
        return
    for q in rows:
        p = by_id.get(str(q.id)) or {}
        reworded = (p.get("reworded_text") or "").strip()
        points = _strings(p.get("expected_points"))
        spoken = (p.get("spoken_text") or "").strip() or reworded
        # unchanged = couldn't be made spoken; the caller serves an MCQ instead of a row left like this
        reason = ("unchanged" if not points or not reworded or reworded == q.text.strip()
                  else speakable.spoken_problem(spoken, p.get("hints")))
        if reason:
            logger.warning("practice_voice_prep_not_reworded", extra={"practice_question_id": str(q.id),
                                                                     "reason": reason})
            continue
        q.text = spoken
        q.expected_points = points
        q.misconceptions = _strings(p.get("misconceptions"))


def _strings(value) -> list[str]:
    return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


async def answer_voice_item(session: AsyncSession, session_id: str, item_id: str, user: User, *,
                            audio_base64: str, mime_type: str, token: str) -> dict:
    """{transcript, score, feedback, xp_awarded}. Already answered -> the
    stored result (no new calls). Transcription failure -> 503, nothing saved
    (the app retries). Grading failure -> transcript saved ungraded, no XP."""
    from src.services import daily_practice  # written alongside; imported lazily

    _, item, question = await daily_practice.load_item_for_student(session, session_id, item_id, user)
    if item.kind != "voice":
        raise ValidationError("This question is answered by picking an option, not by voice.")
    if item.answered_at is not None:
        return _result(item)
    _decode_audio(audio_base64)

    try:
        transcript = await generation_client.transcribe(audio_base64, mime_type, await _vocabulary(session, question), language="auto")
    except AppError as exc:
        logger.warning("practice_voice_transcribe_failed", extra={"item_id": str(item.id), "reason": str(exc)})
        raise TranscriptionUnavailableError("We couldn't process your recording - please try again.") from exc
    if not transcript:
        raise ValidationError("We couldn't hear an answer - please record again.")

    subject = await _subject_name(session, question.chapter_id)
    try:
        graded = await colearner_client.grade_answer(
            question=question.text, expected_points=await _expected_points(session, question),
            misconceptions=list(question.misconceptions or []), transcript=transcript, subject=subject, token=token)
        score = max(0, min(100, int(graded["score"])))
        feedback = {"good": str(graded.get("good") or ""), "add": str(graded.get("add") or "")}
    except (AppError, KeyError, TypeError, ValueError) as exc:
        logger.warning("practice_voice_grading_failed", extra={"item_id": str(item.id), "reason": str(exc)})
        score, feedback = None, dict(UNGRADED_FEEDBACK)

    item.transcript, item.score, item.feedback = transcript, score, feedback
    item.answered_at = datetime.now(timezone.utc)
    item.xp_awarded = await daily_practice.record_item_result(session, item, score=score) if score is not None else 0
    await session.commit()
    return _result(item)


async def _expected_points(session: AsyncSession, question: PracticeQuestion) -> list[str]:
    """The rubric, or - most rows never got one - the book's answer as the one point
    to check against, so the grader isn't judging from the question alone."""
    if question.expected_points:
        return list(question.expected_points)
    answer = (await session.get(DatasetQuestion, question.source_question_id)
              if question.source_question_id else None)
    reference = ((answer.answer if answer else None) or question.explanation or "").strip()
    return [f"Reference answer: {reference[:1500]}"] if reference else []


def _result(item) -> dict:
    return {"transcript": item.transcript, "score": item.score,
            "feedback": item.feedback or dict(UNGRADED_FEEDBACK), "xp_awarded": item.xp_awarded or 0}


async def _subject_name(session: AsyncSession, chapter_id: uuid.UUID) -> str | None:
    chapter = await session.get(Chapter, chapter_id)
    subject = await session.get(Subject, chapter.subject_id) if chapter else None
    return subject.name if subject else None


async def _vocabulary(session: AsyncSession, question: PracticeQuestion) -> str:
    """Transcription prompt: the subject/chapter/topic words, so terms come out right.
    Never the question itself - gpt-4o-mini-transcribe is generative: given the
    question, unclear audio came back as a neat written answer to it (graded 100%)."""
    chapter = await session.get(Chapter, question.chapter_id)
    topic = await session.get(Topic, question.topic_id) if question.topic_id else None
    subject = await session.get(Subject, chapter.subject_id) if chapter else None
    about = ", ".join(w for w in [chapter.name if chapter else None, topic.name if topic else None] if w)
    # vocabulary, not a sentence: on silence the transcriber can echo it (generation drops echoes)
    return "; ".join(w for w in [subject.name if subject else None, about] if w)[:900]
