"""The teacher's report on one challenge - a Speak with Milo session (voice
test) or Fix Milo's homework (quiz test) - in the same shape for both:

- who finished, who is part-way, who hasn't started;
- each topic of the chapter: the class's score and who is struggling on it,
  weakest first (with the NCERT page to re-teach from);
- each thinking skill: the class's score;
- the questions that went worst: how many got them, the misconceptions and
  wrong answers behind them, the hints they needed;
- each student: score, weakest topic and skill, homework and re-check;
- what to do next, in plain sentences (rules on the numbers - no model call).

Everything is read from stored results; nothing is generated.
"""

import uuid
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    BloomLevel,
    Chapter,
    DailyPracticeItem,
    DailyPracticeSession,
    Homework,
    PracticeQuestion,
    QuizTest,
    SchoolClass,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentTestResult,
    StudentTestResultBloomScore,
    StudentTestResultTopicScore,
    Topic,
    User,
    VoiceTest,
    VoiceTestTargetStudent,
)
from src.domain.models.mcq_test import McqTest
from src.services import daily_practice

STRUGGLING_BELOW = 60  # a student under this on a topic is struggling with it
WEAK_CLASS_BELOW = 60  # a class average under this means re-teach
MAX_QUESTIONS = 6
SKILL_NAMES = {
    "REMEMBER": "Remember it", "UNDERSTAND": "Explain it", "APPLY": "Use it",
    "ANALYZE": "Spot patterns", "EVALUATE": "Judge it", "CREATE": "Make your own",
}
VOICE_LEVELS = {"Remembering": "REMEMBER", "Understanding": "UNDERSTAND", "Applying": "APPLY",
                "Analyzing": "ANALYZE", "Evaluating": "EVALUATE", "Creating": "CREATE"}


def _avg(values) -> int | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def _class_label(c: SchoolClass | None) -> str:
    return f"Class {c.grade}{c.section}" if c else ""


async def _topics(session: AsyncSession, chapter_id) -> dict[uuid.UUID, Topic]:
    rows = (await session.execute(select(Topic).where(Topic.chapter_id == chapter_id))).scalars().all()
    return {t.id: t for t in rows}


def _topic_rows(scores: dict[uuid.UUID, dict[uuid.UUID, int]], topics, names) -> list[dict]:
    """scores: topic -> student -> percent. Weakest topic first."""
    out = []
    for topic_id, by_student in scores.items():
        topic = topics.get(topic_id)
        if topic is None or not by_student:
            continue
        out.append({
            "topic_id": str(topic_id), "name": daily_practice.tidy(topic.name), "page": topic.page,
            "percent": _avg(by_student.values()), "students": len(by_student),
            "struggling": sorted(names[s] for s, p in by_student.items() if p < STRUGGLING_BELOW and s in names),
        })
    return sorted(out, key=lambda t: t["percent"])


def _actions(report: dict) -> list[str]:
    """What to do next, from the numbers."""
    acts = []
    weakest = [t for t in report["topics"] if t["percent"] < WEAK_CLASS_BELOW]
    for t in weakest[:2]:
        page = f" (NCERT p. {t['page']})" if t["page"] else ""
        acts.append(f"Re-teach {t['name']}{page} to the class - the class is at {t['percent']}%.")
    for t in report["topics"]:
        if t not in weakest and t["struggling"] and len(t["struggling"]) <= max(3, report["finished"] // 3):
            acts.append(f"Take a small group on {t['name']}: {', '.join(t['struggling'][:5])}.")
            break
    low = [s for s in report["skills"] if s["percent"] is not None and s["percent"] < WEAK_CLASS_BELOW]
    if low:
        s = min(low, key=lambda x: x["percent"])
        acts.append(f"Practise '{s['name']}' questions - the class's weakest thinking skill ({s['percent']}%).")
    if report["questions"] and report["questions"][0]["percent"] < 40:
        q = report["questions"][0]
        text = q["text"] if len(q["text"]) <= 90 else q["text"][:90].rsplit(" ", 1)[0] + "…"
        got = "nobody got it" if q["percent"] == 0 else f"only {q['percent']}% got it"
        acts.append(f"Go over \"{text}\" together - {got}.")
    if report["not_started"]:
        n = len(report["not_started"])
        acts.append(f"{n} haven't started yet ({', '.join(report['not_started'][:4])}{'…' if n > 4 else ''}) - nudge them.")
    pending = [s["name"] for s in report["students"] if s["homework"] == "pending"]
    if pending:
        acts.append(f"{len(pending)} still have their homework to finish before the re-check.")
    if not acts and report["finished"]:
        acts.append("The class is on track with this chapter - move on.")
    return acts


def _finish(report: dict) -> dict:
    report["actions"] = _actions(report)
    return report


async def _homework_status(session: AsyncSession, homework_ids_by_student: dict) -> dict:
    """student -> none | pending | done"""
    ids = [h for h in homework_ids_by_student.values() if h]
    rows = dict(((r[0], r[1]), r[2]) for r in (await session.execute(
        select(StudentHomeworkProgress.homework_id, StudentHomeworkProgress.student_id, StudentHomeworkProgress.status)
        .where(StudentHomeworkProgress.homework_id.in_(ids)))).all()) if ids else {}
    out = {}
    for student, hw in homework_ids_by_student.items():
        status = rows.get((hw, student)) if hw else None
        out[student] = "none" if status is None else "done" if status == StudentHomeworkStatus.COMPLETED else "pending"
    return out


# ------------------------------------------------------------------ Speak with Milo
async def voice_report(session: AsyncSession, test: VoiceTest) -> dict:
    school_class = await session.get(SchoolClass, test.class_id)
    chapter = await session.get(Chapter, test.chapter_id)
    topics = await _topics(session, test.chapter_id)
    assigned = (await session.execute(
        select(User.id, User.display_name).join(VoiceTestTargetStudent, VoiceTestTargetStudent.student_id == User.id)
        .where(VoiceTestTargetStudent.test_id == test.id).order_by(User.display_name))).all()
    names = {sid: name for sid, name in assigned}
    results = {r.student_id: r for r in (await session.execute(
        select(StudentTestResult).where(StudentTestResult.test_id == test.id))).scalars().all()}

    topic_scores: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
    student_topics: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
    skill_scores: dict[str, list[int]] = defaultdict(list)
    student_skills: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    by_result = {r.id: r.student_id for r in results.values()}
    if by_result:
        for rid, topic_id, pct in (await session.execute(
                select(StudentTestResultTopicScore.student_test_result_id, StudentTestResultTopicScore.topic_id,
                       StudentTestResultTopicScore.percent)
                .where(StudentTestResultTopicScore.student_test_result_id.in_(list(by_result))))).all():
            topic_scores[topic_id][by_result[rid]] = pct
            student_topics[by_result[rid]][topic_id] = pct
        for rid, level, pct in (await session.execute(
                select(StudentTestResultBloomScore.student_test_result_id, StudentTestResultBloomScore.bloom_level,
                       StudentTestResultBloomScore.percent)
                .where(StudentTestResultBloomScore.student_test_result_id.in_(list(by_result)),
                       StudentTestResultBloomScore.percent.is_not(None)))).all():
            skill_scores[level.value].append(pct)
            student_skills[by_result[rid]][level.value] = pct

    # the graded conversation, question by question
    questions: dict[int, dict] = {}
    hints: Counter = Counter()
    started = set()
    graded: dict[uuid.UUID, list] = {}  # each student's latest graded session - the one their result shows
    for student_id, report, needs_review in (await session.execute(
            select(AiAssessorSession.student_id, AiAssessorSessionRecord.report, AiAssessorSessionRecord.needs_review)
            .join(AiAssessorSessionRecord, AiAssessorSessionRecord.session_id == AiAssessorSession.id)
            .where(AiAssessorSession.test_id == test.id)
            .order_by(AiAssessorSessionRecord.created_at.desc()))).all():
        started.add(student_id)
        qa_rows = (report or {}).get("qa_transcripts") if not needs_review else None
        if qa_rows and student_id not in graded:
            graded[student_id] = qa_rows
    for student_id, qa_rows in graded.items():
        for qa in qa_rows:
            n = qa.get("question_number") or 0
            q = questions.setdefault(n, {"text": qa.get("question_asked") or "", "scores": [], "misconceptions": [],
                                         "hints": 0, "answered": 0,
                                         "level": VOICE_LEVELS.get(qa.get("blooms_attribute") or "", "")})
            q["scores"].append(qa.get("score_percent") or 0)
            q["answered"] += bool(str(qa.get("student_response") or "").strip())  # "No answer" on the result isn't one
            if note := str(qa.get("misconception") or "").strip().lstrip("-•* ").strip():
                q["misconceptions"].append(note)
            q["hints"] += qa.get("hints_used") or 0
            hints[student_id] += qa.get("hints_used") or 0

    homework = (await session.execute(select(Homework.id).where(Homework.test_id == test.id))).scalars().all()
    hw_by_student = {}
    if homework:
        for hw_id, sid in (await session.execute(
                select(StudentHomeworkProgress.homework_id, StudentHomeworkProgress.student_id)
                .where(StudentHomeworkProgress.homework_id.in_(homework)))).all():
            hw_by_student[sid] = hw_id
    hw_status = await _homework_status(session, {sid: hw_by_student.get(sid) for sid in names})
    rechecks = dict((await session.execute(select(McqTest.student_id, McqTest.score_percent).where(
        McqTest.voice_test_id == test.id, McqTest.completed_at.is_not(None)))).all())

    students = []
    for sid, name in assigned:
        r = results.get(sid)
        weakest_topic = min(student_topics[sid].items(), key=lambda kv: kv[1], default=(None, None))[0]
        weakest_skill = min(student_skills[sid].items(), key=lambda kv: kv[1], default=(None, None))[0]
        students.append({
            "student_id": str(sid), "name": name,
            "status": "done" if r else "in_progress" if sid in started else "not_started",
            "score": round(r.mastery_percent) if r and r.mastery_percent is not None else None,
            "weakest_topic": daily_practice.tidy(topics[weakest_topic].name) if weakest_topic in topics else None,
            "weakest_skill": SKILL_NAMES.get(weakest_skill or ""),
            "hints_used": hints[sid], "homework": hw_status.get(sid, "none"), "recheck_score": rechecks.get(sid),
            "topic_scores": {str(t): p for t, p in student_topics[sid].items()},
        })
    return _finish(_base(test, "speak", school_class, chapter, students, topic_scores, topics, names,
                         skill_scores, [
                             {"text": q["text"], "level": SKILL_NAMES.get(q["level"], ""), "percent": _avg(q["scores"]),
                              "answered": q["answered"], "hints": q["hints"],
                              "notes": [m for m, _ in Counter(q["misconceptions"]).most_common(3)]}
                             for _, q in sorted(questions.items())]))


# ------------------------------------------------------------------ Fix Milo's homework
async def quiz_report(session: AsyncSession, test: QuizTest) -> dict:
    from src.services import quiz_tests

    school_class = await session.get(SchoolClass, test.class_id)
    chapter = await session.get(Chapter, test.chapter_id)
    topics = await _topics(session, test.chapter_id)
    r = await quiz_tests.results(session, test)
    names = {uuid.UUID(s["student_id"]): s["name"] for s in r["students"]}
    attempts = await quiz_tests.attempts(session, test)

    topic_scores: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
    student_topics: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
    questions: dict[uuid.UUID, dict] = {}
    session_student = {a.id: a.student_id for a in attempts.values()}
    if session_student:
        rows = (await session.execute(
            select(DailyPracticeItem, PracticeQuestion)
            .join(PracticeQuestion, PracticeQuestion.id == DailyPracticeItem.practice_question_id)
            .where(DailyPracticeItem.session_id.in_(list(session_student)),
                   DailyPracticeItem.answered_at.is_not(None)))).all()
        per_topic: dict[tuple, list[bool]] = defaultdict(list)
        for item, q in rows:
            sid = session_student[item.session_id]
            if item.topic_id is not None:
                per_topic[(item.topic_id, sid)].append(bool(item.correct))
            entry = questions.setdefault(q.id, {"text": q.text, "level": item.bloom_level.value, "right": 0,
                                                "answered": 0, "wrong": Counter(), "notes": Counter()})
            entry["answered"] += 1
            if item.correct:
                entry["right"] += 1
            else:
                if q.options and item.option_index is not None and 0 <= item.option_index < len(q.options):
                    entry["wrong"][q.options[item.option_index]] += 1
                mistake = (item.feedback or {}).get("mistake")
                if mistake:
                    entry["notes"][str(mistake)] += 1
        for (topic_id, sid), marks in per_topic.items():
            pct = round(100 * sum(marks) / len(marks))
            topic_scores[topic_id][sid] = pct
            student_topics[sid][topic_id] = pct

    rechecks = dict((await session.execute(select(DailyPracticeSession.student_id, DailyPracticeSession.score_percent)
                                            .where(DailyPracticeSession.quiz_test_id == test.id,
                                                   DailyPracticeSession.attempt == 2,
                                                   DailyPracticeSession.outcome.is_not(None)))).all())
    hw_status = await _homework_status(session, {sid: (attempts[sid].homework_id if sid in attempts else None)
                                                 for sid in names})
    skill_scores: dict[str, list[int]] = defaultdict(list)
    students = []
    for s in r["students"]:
        sid = uuid.UUID(s["student_id"])
        per_bloom = s["per_bloom"] or {}
        for level, pct in per_bloom.items():
            skill_scores[level].append(pct)
        weakest_topic = min(student_topics[sid].items(), key=lambda kv: kv[1], default=(None, None))[0]
        weakest_skill = min(per_bloom.items(), key=lambda kv: kv[1], default=(None, None))[0]
        students.append({
            "student_id": s["student_id"], "name": s["name"], "status": s["status"], "score": s["score_percent"],
            "weakest_topic": daily_practice.tidy(topics[weakest_topic].name) if weakest_topic in topics else None,
            "weakest_skill": SKILL_NAMES.get(weakest_skill or ""), "hints_used": 0,
            "homework": hw_status.get(sid, "none"), "recheck_score": rechecks.get(sid),
            "topic_scores": {str(t): p for t, p in student_topics[sid].items()},
        })
    return _finish(_base(test, "fix", school_class, chapter, students, topic_scores, topics, names, skill_scores, [
        {"text": q["text"], "level": SKILL_NAMES.get(q["level"], ""),
         "percent": round(100 * q["right"] / q["answered"]), "answered": q["answered"], "hints": 0,
         "notes": [f"Picked \"{w}\"" for w, _ in q["wrong"].most_common(2)]
                  + [m for m, _ in q["notes"].most_common(2)]}
        for q in questions.values() if q["answered"]]))


def _base(test, kind, school_class, chapter, students, topic_scores, topics, names, skill_scores, questions) -> dict:
    done = [s for s in students if s["status"] == "done"]
    return {
        "kind": kind,
        "title": chapter.name if kind == "speak" and chapter else getattr(test, "title", "") or "",
        "class_label": _class_label(school_class),
        "chapter": chapter.name if chapter else "",
        "assigned": len(students),
        "finished": len(done),
        "in_progress": sum(s["status"] == "in_progress" for s in students),
        "not_started": [s["name"] for s in students if s["status"] == "not_started"],
        "average_percent": _avg(s["score"] for s in done),
        "topics": _topic_rows(topic_scores, topics, names),
        "skills": [{"level": level.value, "name": SKILL_NAMES[level.value],
                    "percent": _avg(skill_scores.get(level.value, []))}
                   for level in BloomLevel if skill_scores.get(level.value)],
        "questions": sorted([q for q in questions if q["percent"] is not None],
                            key=lambda q: q["percent"])[:MAX_QUESTIONS],
        "students": sorted(students, key=lambda s: (s["status"] != "done", s["score"] if s["score"] is not None else 101)),
    }
