"""A compact, factual snapshot of one teacher's world - the only thing the
teacher assistant and the dashboard summary are allowed to talk about.

Everything here is read from the database (no model output), small enough
to send with every chat turn, and scoped exactly like GET /tests: the
classes + subjects this teacher teaches, in their school.
"""

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    Chapter,
    Homework,
    QuestionPaper,
    QuizTest,
    SchoolClass,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentProfile,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    TeacherClassAssignment,
    User,
    VoiceTest,
)

RECENT_TESTS = 8
STRUGGLING = 5


def _class_label(c: SchoolClass) -> str:
    return f"Class {c.grade}{c.section}"


async def build_teacher_facts(session: AsyncSession, teacher: User) -> dict:
    assignments = (await session.execute(
        select(SchoolClass, Subject)
        .join(TeacherClassAssignment, TeacherClassAssignment.class_id == SchoolClass.id)
        .join(Subject, Subject.id == TeacherClassAssignment.subject_id)
        .where(TeacherClassAssignment.teacher_id == teacher.id, SchoolClass.school_id == teacher.school_id)
    )).all()
    class_ids = {c.id for c, _ in assignments}
    students_per_class = dict((await session.execute(
        select(StudentProfile.class_id, func.count()).where(StudentProfile.class_id.in_(class_ids))
        .group_by(StudentProfile.class_id)
    )).all()) if class_ids else {}
    classes = sorted({(_class_label(c), s.name, students_per_class.get(c.id, 0)) for c, s in assignments})

    tests = (await session.execute(
        select(VoiceTest, SchoolClass, Chapter)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .join(Chapter, Chapter.id == VoiceTest.chapter_id)
        .join(TeacherClassAssignment, and_(
            TeacherClassAssignment.class_id == VoiceTest.class_id,
            TeacherClassAssignment.subject_id == VoiceTest.subject_id,
            TeacherClassAssignment.teacher_id == teacher.id))
        .where(SchoolClass.school_id == teacher.school_id)
        .order_by(VoiceTest.created_at.desc())
        .limit(RECENT_TESTS)
    )).all()
    test_ids = [t.id for t, _, _ in tests]

    avg_mastery = dict((await session.execute(
        select(StudentTestResult.test_id, func.round(func.avg(StudentTestResult.mastery_percent)))
        .where(StudentTestResult.test_id.in_(test_ids)).group_by(StudentTestResult.test_id)
    )).all()) if test_ids else {}
    bloom_rows = (await session.execute(
        select(StudentTestResultBloomScore.bloom_level, func.round(func.avg(StudentTestResultBloomScore.percent)))
        .join(StudentTestResult, StudentTestResult.id == StudentTestResultBloomScore.student_test_result_id)
        .where(StudentTestResult.test_id.in_(test_ids), StudentTestResultBloomScore.percent.is_not(None))
        .group_by(StudentTestResultBloomScore.bloom_level)
    )).all() if test_ids else []
    pending_reviews = (await session.execute(
        select(func.count()).select_from(AiAssessorSessionRecord)
        .join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.test_id.in_(test_ids), AiAssessorSessionRecord.needs_review.is_(True))
    )).scalar_one() if test_ids else 0

    now = datetime.now(timezone.utc)
    recent_tests = [{
        "chapter": chapter.name,
        "class": _class_label(klass),
        "status": test.status.value,
        "finished": f"{test.completed_count}/{test.assigned_count}",
        "class_average_percent": int(avg_mastery[test.id]) if test.id in avg_mastery else None,
        "deadline_passed": bool(test.due_at and test.due_at < now),
    } for test, klass, chapter in tests]

    struggling = (await session.execute(
        select(User.display_name, func.round(func.avg(StudentTestResult.mastery_percent)).label("avg"))
        .join(StudentTestResult, StudentTestResult.student_id == User.id)
        .where(StudentTestResult.test_id.in_(test_ids))
        .group_by(User.id, User.display_name)
        .order_by("avg").limit(STRUGGLING)
    )).all() if test_ids else []

    homework_rows = (await session.execute(
        select(Homework.gap_topic, StudentHomeworkProgress.status, func.count())
        .join(StudentHomeworkProgress, StudentHomeworkProgress.homework_id == Homework.id)
        .where(Homework.test_id.in_(test_ids))
        .group_by(Homework.gap_topic, StudentHomeworkProgress.status)
    )).all() if test_ids else []
    homework: dict[str, dict[str, int]] = defaultdict(lambda: {"done": 0, "total": 0})
    for topic, status, count in homework_rows:
        homework[topic]["total"] += count
        if status == StudentHomeworkStatus.COMPLETED:
            homework[topic]["done"] += count

    papers = (await session.execute(
        select(QuestionPaper.name, QuestionPaper.status)
        .where(QuestionPaper.created_by == teacher.id)
        .order_by(QuestionPaper.created_at.desc()).limit(5)
    )).all()

    from src.services import quiz_tests  # lazy: quiz_tests pulls in the practice services

    challenges = []
    for test in (await session.execute(
            select(QuizTest).where(QuizTest.created_by == teacher.id, QuizTest.status == "published")
            .order_by(QuizTest.created_at.desc()).limit(5))).scalars():
        r = await quiz_tests.results(session, test)
        challenges.append({
            "title": test.title, "finished": f"{r['done']}/{r['student_count']}",
            "class_average_percent": r["average_percent"], "deadline_passed": bool(test.due_at and test.due_at < now),
            "students_needing_help": r["needs_help"][:STRUGGLING]})

    return {
        "teacher": teacher.display_name,
        "today": now.date().isoformat(),
        "classes": [{"class": c, "subject": s, "students": n} for c, s, n in classes],
        "recent_voice_tests": recent_tests,
        "open_challenges": challenges,  # e.g. Fix Milo's homework (Maths)
        "thinking_levels_average_percent": {lvl.value: int(p) for lvl, p in bloom_rows},
        "attempts_waiting_for_teacher_review": pending_reviews,
        "students_needing_help": [{"name": n, "average_percent": int(a)} for n, a in struggling],
        "homework_completion": [{"topic": t, **v} for t, v in list(homework.items())[:6]],
        "recent_question_papers": [{"name": n, "status": s.value} for n, s in papers],
    }
