"""Post-test insights (contract C4, GET /me/tests/{id}/insights): where the
student lags, in their own words next to the key idea.

Every number is computed here from stored data - the StudentTestResult,
its per-Bloom and per-topic scores, and the session record's raw grader
report (qa_transcripts). Colearner's POST /api/v1/insights only writes the
words (headline, a key idea per card, next steps, spoken recap); when it is
unreachable the same numbers go out with template words, never a 500.
Colearner's words are stored on the result (insights_words) with the
fingerprint of their inputs and only rewritten when that changes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import undefer
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError

from src.config.settings import get_settings
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    BloomLevel,
    Chapter,
    SchoolClass,
    StudentTestResult,
    StudentTestResultBloomScore,
    StudentTestResultTopicScore,
    Subject,
    Subtopic,
    Topic,
    VoiceTest,
    VoiceTestQuestion,
)

logger = logging.getLogger(__name__)

CARD_SCORE_BELOW = 75
_TIMEOUT_SECONDS = 30.0


def topic_status(percent: int) -> str:
    return "strong" if percent >= 75 else "ok" if percent >= 50 else "weak"


def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def _latest_report(records: list[AiAssessorSessionRecord]) -> tuple[AiAssessorSessionRecord | None, list[dict]]:
    """The newest graded session record of this student on this test."""
    for record in records:
        qa = (record.report or {}).get("qa_transcripts") if isinstance(record.report, dict) else None
        if isinstance(qa, list) and any(isinstance(e, dict) and isinstance(e.get("score_percent"), (int, float)) for e in qa):
            return record, [e for e in qa if isinstance(e, dict)]
    return None, []


def build_cards(qa: list[dict], questions: dict[int, VoiceTestQuestion], answers: list | None = None) -> list[dict]:
    """Answers scoring under CARD_SCORE_BELOW, in question order. missed_points =
    the question's expected points the grader didn't list as covered. you_said is
    the stored transcript of the answer (answers[n].first_answer, as the teacher view
    shows it), not the grader's student_response, which the model writes."""
    heard = {a.get("question_number"): a.get("first_answer") for a in answers or [] if isinstance(a, dict)}
    cards = []
    for entry in sorted(qa, key=lambda e: e.get("question_number") or 0):
        score = entry.get("score_percent")
        if not isinstance(score, (int, float)) or score >= CARD_SCORE_BELOW:
            continue
        number = entry.get("question_number")
        question = questions.get(number)
        covered = {_norm(p) for p in entry.get("points_covered") or []}
        expected = list(question.expected_points or []) if question else []
        cards.append({
            "question_number": number if isinstance(number, int) else 0,
            "score_percent": round(score),
            "question_text": (question.reworded_text if question else None) or str(entry.get("question_asked") or ""),
            "you_said": " ".join(str(heard.get(number) or entry.get("student_response") or "").split()),
            "expected_points": expected,
            "missed_points": [p for p in expected if _norm(p) not in covered],
            "topic_id": question.topic_id if question else None,
            "subtopic_id": question.subtopic_id if question else None,
        })
    return cards


def template_words(per_bloom: list[dict], topics: list[dict], cards: list[dict], chapter: str | None) -> dict:
    top = max(per_bloom, key=lambda b: b["percent"]) if per_bloom else None
    low = min(per_bloom, key=lambda b: b["percent"]) if per_bloom else None
    if top is None:
        headline = "Here is how your voice test went."
    elif top["percent"] == 0:
        headline = "A tough one - let's build it up together."
    elif top["percent"] == low["percent"]:
        # one level as both "strongest" and "grow most" read as nonsense
        headline = f"You scored {top['percent']}% at every level."
    else:
        headline = (f"You are strongest at {top['level'].value.title()} "
                    f"and can grow most at {low['level'].value.title()}.")
    refs = [t["name"] for t in sorted(topics, key=lambda t: t["percent"])] or [chapter or "your textbook chapter"]
    next_steps = [
        {"title": "Re-read the key idea and say it aloud in your own words", "reference": refs[0]},
        {"title": "Try your homework questions on this", "reference": refs[min(1, len(refs) - 1)]},
        {"title": "Explain it to a friend or family member", "reference": refs[min(2, len(refs) - 1)]},
    ]
    return {
        "headline": headline,
        "key_ideas": {
            c["question_number"]: (c["missed_points"] or c["expected_points"] or ["Look at this idea again in your textbook."])[0]
            for c in cards
        },
        "next_steps": next_steps,
        "recap_text": (
            "Well done for finishing your voice test! You shared a lot of good thinking. "
            "Take a look at the cards below, they show a few ideas worth another look. "
            "Do your homework on them, and you will see yourself grow in the next test."
        ),
    }


async def _colearner_words(facts: dict, token: str | None) -> dict:
    """{headline, cards:[{question_number, key_idea}], next_steps, recap_text} - raises on any failure."""
    base_url = (get_settings().colearner_service_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("COLEARNER_SERVICE_URL is not configured.")
    async with httpx.AsyncClient(base_url=base_url, timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(
            "/api/v1/insights", json=facts,
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )
        response.raise_for_status()
        data = response.json()
    return {
        "headline": data["headline"],
        "key_ideas": {c["question_number"]: c["key_idea"] for c in data.get("cards") or []},
        "next_steps": data["next_steps"][:3],
        "recap_text": data["recap_text"],
    }


def _fingerprint(result: StudentTestResult, record: AiAssessorSessionRecord | None, cards: list[dict]) -> list:
    # records (keyed by session_id) have no updated_at; created_at stands in
    return [str(result.id), result.mastery_percent, str(record.session_id) if record else None,
            record.created_at.isoformat() if record and record.created_at else None,
            [c["question_number"] for c in cards]]


async def build_test_insights(session: AsyncSession, *, test_id: str, student_id: uuid.UUID, token: str | None) -> dict:
    try:
        test_uuid = uuid.UUID(str(test_id))
    except ValueError:
        raise NotFoundError("You haven't attempted this Test.") from None
    result = (await session.execute(select(StudentTestResult).options(undefer(StudentTestResult.insights_words)).where(
        StudentTestResult.test_id == test_uuid, StudentTestResult.student_id == student_id
    ))).scalar_one_or_none()
    if result is None:
        raise NotFoundError("You haven't attempted this Test.")

    order = {level: i for i, level in enumerate(BloomLevel)}
    per_bloom = sorted(
        ({"level": s.bloom_level, "percent": s.percent} for s in (await session.execute(
            select(StudentTestResultBloomScore).where(StudentTestResultBloomScore.student_test_result_id == result.id)
        )).scalars() if s.percent is not None),
        key=lambda b: order[b["level"]],
    )
    topics = [
        {"topic_id": str(topic_id), "name": name, "percent": percent, "status": topic_status(percent)}
        for topic_id, name, percent in (await session.execute(
            select(StudentTestResultTopicScore.topic_id, Topic.name, StudentTestResultTopicScore.percent)
            .join(Topic, Topic.id == StudentTestResultTopicScore.topic_id)
            .where(StudentTestResultTopicScore.student_test_result_id == result.id)
            .order_by(StudentTestResultTopicScore.percent)
        )).all()
    ]

    records = list((await session.execute(
        select(AiAssessorSessionRecord)
        .join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.test_id == test_uuid, AiAssessorSession.student_id == student_id,
               AiAssessorSessionRecord.needs_review.is_(False))
        .order_by(AiAssessorSessionRecord.created_at.desc())
    )).scalars())
    record, qa = _latest_report(records)
    questions = {q.order: q for q in (await session.execute(
        select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test_uuid)
    )).scalars()}
    cards = build_cards(qa, questions, record.answers if record else None)

    names: dict[uuid.UUID, str] = {}
    topic_ids = {c["topic_id"] for c in cards} - {None}
    subtopic_ids = {c["subtopic_id"] for c in cards} - {None}
    if topic_ids:
        names.update((await session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids)))).all())
    if subtopic_ids:
        names.update((await session.execute(select(Subtopic.id, Subtopic.name).where(Subtopic.id.in_(subtopic_ids)))).all())

    test = await session.get(VoiceTest, test_uuid)
    chapter = await session.get(Chapter, test.chapter_id) if test else None
    subject = await session.get(Subject, test.subject_id) if test else None
    school_class = await session.get(SchoolClass, test.class_id) if test else None

    fingerprint = _fingerprint(result, record, cards)
    to_store = None  # template words aren't stored, so colearner is retried next time
    stored = result.insights_words or {}
    if stored.get("fingerprint") == fingerprint:
        words = {**stored["words"], "key_ideas": {int(k): v for k, v in stored["words"]["key_ideas"].items()}}
        generated_at = datetime.fromisoformat(stored["generated_at"])
    else:
        facts = {
            "grade": f"Class {school_class.grade}" if school_class else None,
            "subject": subject.name if subject else None,
            "chapter": chapter.name if chapter else None,
            "mastery_percent": result.mastery_percent,
            "per_bloom": [{"level": b["level"].value, "percent": b["percent"]} for b in per_bloom],
            "topics": [{"name": t["name"], "percent": t["percent"]} for t in topics],
            "cards": [{
                "question_number": c["question_number"], "question_text": c["question_text"],
                "student_response": c["you_said"], "expected_points": c["expected_points"],
                "missed_points": c["missed_points"],
                "topic": names.get(c["topic_id"]), "subtopic": names.get(c["subtopic_id"]),
            } for c in cards],
        }
        generated_at = datetime.now(timezone.utc)
        try:
            words = await _colearner_words(facts, token)
            to_store = {"fingerprint": fingerprint, "words": words, "generated_at": generated_at.isoformat()}
        except Exception as exc:  # any failure -> template words, never a 500
            logger.warning("insights_words_fallback", extra={"test_id": str(test_uuid), "reason": str(exc)[:300]})
            words = template_words(per_bloom, topics, cards, chapter.name if chapter else None)

    fallback = template_words(per_bloom, topics, cards, chapter.name if chapter else None)
    out = {
        "test_id": str(test_uuid),
        "headline": words["headline"],
        "mastery_percent": result.mastery_percent,
        "per_bloom": per_bloom,
        "topics": topics,
        "cards": [{
            "question_number": c["question_number"], "question_text": c["question_text"], "you_said": c["you_said"],
            "score_percent": c["score_percent"],
            # the key idea is often a missed point word for word (always in the template): show it once
            "missed_points": [p for p in c["missed_points"] if _norm(p) != _norm(key_idea)],
            "key_idea": key_idea,
        } for c in cards
            for key_idea in [words["key_ideas"].get(c["question_number"]) or fallback["key_ideas"][c["question_number"]]]],
        "next_steps": (list(words["next_steps"]) + fallback["next_steps"])[:3],
        "recap_text": words["recap_text"],
        "generated_at": generated_at,
    }
    if to_store:
        result.insights_words = to_store
        await session.commit()
    return out
