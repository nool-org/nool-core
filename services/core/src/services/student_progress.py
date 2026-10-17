"""A student's progress per subject, from everything they've finished: chats
with Milo (StudentTestResult), Milo's check-ins and teacher challenges such
as Fix Milo's homework (finished practice sessions with a topic or a quiz
test). Per subject: the average score, each thinking level's average, the
weakest level to work on, and the results newest first."""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    BloomLevel,
    Chapter,
    DailyPracticeSession,
    QuizTest,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    Topic,
    User,
    VoiceTest,
)
from src.services import daily_practice

WEAK_BELOW = 70  # a level under this is worth working on (topic_quiz.LAGGING_BELOW)


def _kind(session: DailyPracticeSession, test: QuizTest | None) -> str:
    """The names the app uses: a topic checkpoint, a teacher's challenge, and either's re-check."""
    if session.attempt == 2:
        return "Re-check"
    if test is not None:
        return "Fix Milo's homework" if set(test.mix or {}) == {"fix_steps"} else "Challenge"
    return "Topic checkpoint"


async def by_subject(session: AsyncSession, student: User) -> list[dict]:
    results: dict[uuid.UUID, list[dict]] = defaultdict(list)
    names: dict[uuid.UUID, str] = {}

    voice = (await session.execute(
        select(StudentTestResult.id, StudentTestResult.test_id, StudentTestResult.mastery_percent,
               VoiceTest.created_at, Subject.id, Subject.name, Chapter.name)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .join(Subject, Subject.id == VoiceTest.subject_id)
        .outerjoin(Chapter, Chapter.id == VoiceTest.chapter_id)
        .where(StudentTestResult.student_id == student.id))).all()
    levels: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    if voice:
        for rid, level, percent in (await session.execute(
                select(StudentTestResultBloomScore.student_test_result_id, StudentTestResultBloomScore.bloom_level,
                       StudentTestResultBloomScore.percent)
                .where(StudentTestResultBloomScore.student_test_result_id.in_([v[0] for v in voice])))).all():
            if percent is not None:
                levels[rid][level.value] = percent
    for rid, test_id, mastery, at, sid, sname, chapter in voice:
        names[sid] = sname
        results[sid].append({"label": chapter or sname, "kind": "Speak with Milo", "score_percent": round(mastery or 0),
                             "per_bloom": levels.get(rid, {}), "at": at, "test_id": str(test_id), "quiz_id": None})

    finished = (await session.execute(
        select(DailyPracticeSession, Subject.id, Subject.name, Topic.name, QuizTest)
        .join(Chapter, Chapter.id == DailyPracticeSession.chapter_id)
        .join(Subject, Subject.id == Chapter.subject_id)
        .outerjoin(Topic, Topic.id == DailyPracticeSession.topic_id)
        .outerjoin(QuizTest, QuizTest.id == DailyPracticeSession.quiz_test_id)
        .where(DailyPracticeSession.student_id == student.id, DailyPracticeSession.outcome.is_not(None),
               (DailyPracticeSession.topic_id.is_not(None)) | (DailyPracticeSession.quiz_test_id.is_not(None))))).all()
    for quiz, sid, sname, topic, test in finished:
        names[sid] = sname
        results[sid].append({
            "label": test.title if test is not None else daily_practice.tidy(topic or sname),
            "kind": _kind(quiz, test), "score_percent": quiz.score_percent or 0, "per_bloom": quiz.per_bloom or {},
            "at": quiz.completed_at or quiz.created_at, "test_id": None, "quiz_id": str(quiz.id)})

    out = []
    for sid, rows in results.items():
        rows.sort(key=lambda r: r["at"], reverse=True)
        skills = []
        for level in BloomLevel:
            marks = [r["per_bloom"][level.value] for r in rows if level.value in r["per_bloom"]]
            skills.append({"level": level, "percent": round(sum(marks) / len(marks)) if marks else None})
        assessed = [s for s in skills if s["percent"] is not None]
        weakest = min(assessed, key=lambda s: s["percent"]) if assessed else None
        out.append({
            "subject_id": str(sid), "subject_label": names[sid],
            "score_percent": round(sum(r["score_percent"] for r in rows) / len(rows)), "count": len(rows),
            "skills": skills,
            "focus_level": weakest["level"] if weakest and weakest["percent"] < WEAK_BELOW else None,
            "focus_percent": weakest["percent"] if weakest and weakest["percent"] < WEAK_BELOW else None,
            "results": [{k: v for k, v in r.items() if k != "per_bloom"} for r in rows[:30]],
        })
    return sorted(out, key=lambda s: s["subject_label"])
