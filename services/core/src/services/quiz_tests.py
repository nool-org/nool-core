"""Teacher-made written tests - for subjects without voice tests (Maths,
subject_rules.py). The teacher picks a chapter's topics and how many of each
question type; the questions are chosen once - the same for every student -
from the shared practice pool (bank, then generation; daily_practice._fill),
reviewed, then published to the class.

A student's attempt is a practice session with quiz_test_id (attempt 1, no
topic): answering, the analysis and the homework on its weakest topic are the
topic quiz's (topic_quiz.finish / build_homework). No re-check: the next step
after a test is its homework.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError, ValidationError
from src.domain.models import (
    BloomLevel,
    Chapter,
    DailyPracticeItem,
    DailyPracticeSession,
    PracticeQuestion,
    QuizTest,
    SchoolClass,
    StudentProfile,
    Subject,
    Topic,
    User,
)
from src.services import ai_context, daily_practice, push, question_types, subject_rules, topic_quiz
from src.services.daily_practice import Slot

logger = logging.getLogger(__name__)

KINDS = ("mcq", *question_types.KINDS)
# the MCQs' levels in turn: mostly problems to work out (Apply), like a board paper
MCQ_LEVELS = [BloomLevel.APPLY, BloomLevel.APPLY, BloomLevel.UNDERSTAND, BloomLevel.APPLY, BloomLevel.ANALYZE,
              BloomLevel.REMEMBER]
KIND_LEVEL = {"who_is_right": BloomLevel.ANALYZE, "fix_steps": BloomLevel.EVALUATE,
              "make_your_own": BloomLevel.CREATE}
MAX_QUESTIONS = 20


def plan(topics: list[Topic], mix: dict[str, int]) -> list[Slot]:
    """One slot per question: the topics in turn, MCQs cycling through
    MCQ_LEVELS (mostly Apply problems), each other type at its own level."""
    unknown = set(mix) - set(KINDS)
    if unknown:
        raise ValidationError(f"Unknown question type: {', '.join(sorted(unknown))}.")
    total = sum(max(0, int(n)) for n in mix.values())
    if not topics or not 1 <= total <= MAX_QUESTIONS:
        raise ValidationError(f"Pick at least one topic and 1-{MAX_QUESTIONS} questions.")
    slots, n = [], 0
    for kind in KINDS:
        for i in range(max(0, int(mix.get(kind, 0)))):
            level = MCQ_LEVELS[i % len(MCQ_LEVELS)] if kind == "mcq" else KIND_LEVEL[kind]
            slots.append(Slot(topics[n % len(topics)], kind, level))
            n += 1
    return slots


async def create(session: AsyncSession, teacher: User, class_id: str, chapter_id: str, topic_ids: list[str],
                 mix: dict[str, int], title: str | None, due_at: datetime | None) -> QuizTest:
    """The test, 'preparing' - prepare() picks its questions. Caller commits, then queues prepare."""
    from src.api.routes.daily_practice import _get, _teaches

    chapter = await _get(session, Chapter, chapter_id, "Chapter")
    class_uuid = await _teaches(session, teacher, class_id, chapter.subject_id)
    topics = [await _get(session, Topic, t, "Topic") for t in topic_ids]
    if any(t.chapter_id != chapter.id for t in topics):
        raise ValidationError("Every topic must be from the chosen chapter.")
    plan(topics, mix)  # validates the mix
    subject = await session.get(Subject, chapter.subject_id)
    unfit = sorted(k for k, n in mix.items() if int(n) > 0 and k not in subject_rules.formats(subject.name))
    if unfit:
        raise ValidationError(f"{subject.name} doesn't use: {', '.join(unfit)}.")
    test = QuizTest(id=uuid.uuid4(), class_id=class_uuid, subject_id=chapter.subject_id, chapter_id=chapter.id,
                    # a default name a teacher can tell apart from last week's: "Real Numbers test · 13 Oct"
                    title=(title or "").strip() or f"{daily_practice.tidy(chapter.name)} challenge · "
                    f"{daily_practice.today_ist().strftime('%d %b').lstrip('0')}",
                    topic_ids=[str(t.id) for t in topics], mix={k: int(v) for k, v in mix.items() if int(v) > 0},
                    question_ids=[], status="preparing", due_at=due_at, created_by=teacher.id)
    session.add(test)
    await session.flush()
    return test


async def prepare(test_id: uuid.UUID) -> None:
    """Picks the test's questions in its own session: pool first, then the bank,
    then generation (a type that can't be made becomes an MCQ on that level).
    'ready' with questions, else 'failed'. Logged, never raised."""
    from src.repositories import get_session

    try:
        async with get_session() as session:
            test = await session.get(QuizTest, test_id)
            if test is None or test.status != "preparing":
                return
            teacher = await session.get(User, test.created_by)
            school_class = await session.get(SchoolClass, test.class_id)
            topics = [await session.get(Topic, uuid.UUID(t)) for t in test.topic_ids]
            with ai_context.feature("quiz_test_questions"):
                chosen = await daily_practice._fill(session, teacher, school_class, plan(topics, test.mix),
                                                    daily_practice.today_ist(), str(test.id), exact_level=True)
            test.question_ids = [str(q.id) for _, q in chosen]
            test.status = "ready" if chosen else "failed"
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("quiz_test_prepare_failed", extra={"quiz_test_id": str(test_id)})


async def own_test(session: AsyncSession, teacher: User, test_id: str) -> QuizTest:
    try:
        test = await session.get(QuizTest, uuid.UUID(test_id))
    except ValueError:
        test = None
    if test is None or test.created_by != teacher.id:
        raise NotFoundError("Test not found.")
    return test


async def questions_of(session: AsyncSession, test: QuizTest) -> list[PracticeQuestion]:
    rows = {str(q.id): q for q in (await session.execute(select(PracticeQuestion).where(
        PracticeQuestion.id.in_([uuid.UUID(i) for i in test.question_ids])))).scalars()} if test.question_ids else {}
    return [rows[i] for i in test.question_ids if i in rows]


async def publish(session: AsyncSession, test: QuizTest, opens_at: datetime | None = None) -> bool:
    """Opens the test to the class now, or schedules it to open at opens_at
    (409 unless its questions are ready). True when it opened now (so the
    class can be told). Caller commits."""
    if test.status in ("published", "scheduled") and opens_at is None:
        return False
    if test.status not in ("ready", "scheduled"):
        raise ConflictError("The questions aren't ready yet.")
    now = datetime.now(timezone.utc)
    later = opens_at is not None and opens_at > now
    test.status, test.published_at = ("scheduled", opens_at) if later else ("published", now)
    await session.flush()
    return not later


def is_open(test: QuizTest, now: datetime | None = None) -> bool:
    """Published, or scheduled and its time has come."""
    now = now or datetime.now(timezone.utc)
    return test.status in ("published", "scheduled") and test.published_at is not None and test.published_at <= now


async def notify_class(session: AsyncSession, test: QuizTest) -> None:
    students = (await session.execute(select(StudentProfile.user_id).where(
        StudentProfile.class_id == test.class_id))).scalars().all()
    await push.notify(session, students, f"New challenge: {test.title}", "Your teacher set you a challenge - start it now.",
                      {"type": "quiz_test", "quizTestId": str(test.id)})


# ------------------------------------------------------------------ students
async def tests_for(session: AsyncSession, student: User) -> list[QuizTest]:
    profile = (await session.execute(select(StudentProfile).where(
        StudentProfile.user_id == student.id))).scalar_one_or_none()
    if profile is None:
        return []
    return list((await session.execute(select(QuizTest).where(
        QuizTest.class_id == profile.class_id, QuizTest.status.in_(("published", "scheduled")),
        QuizTest.published_at <= datetime.now(timezone.utc))  # the app's clock, as publish() stamps it
        .order_by(QuizTest.published_at.desc()))).scalars().all())


async def start(session: AsyncSession, student: User, test_id: str) -> DailyPracticeSession:
    """The student's attempt - built on first call, the same one after. Caller commits."""
    try:
        tid = uuid.UUID(test_id)
    except ValueError:
        raise NotFoundError("Test not found.") from None
    test = await session.get(QuizTest, tid)
    if test is None or not is_open(test) or test not in await tests_for(session, student):
        raise NotFoundError("Test not found.")
    created = (await session.execute(pg_insert(DailyPracticeSession).values(
        id=uuid.uuid4(), student_id=student.id, practice_date=daily_practice.today_ist(),
        chapter_id=test.chapter_id, quiz_test_id=test.id, attempt=1, status="open", xp_awarded=0,
    ).on_conflict_do_nothing(index_elements=["student_id", "quiz_test_id"],
                             index_where=DailyPracticeSession.quiz_test_id.is_not(None))
        .returning(DailyPracticeSession.id))).scalar_one_or_none()
    attempt = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.student_id == student.id, DailyPracticeSession.quiz_test_id == test.id)
        .execution_options(populate_existing=True))).scalar_one()
    if created is not None:
        session.add_all([DailyPracticeItem(session_id=attempt.id, order=n, practice_question_id=q.id, kind=q.kind,
                                           topic_id=q.topic_id, bloom_level=q.bloom_level)
                         for n, q in enumerate(await questions_of(session, test), start=1)])
        await session.flush()
    return attempt


async def attempts(session: AsyncSession, test: QuizTest) -> dict[uuid.UUID, DailyPracticeSession]:
    rows = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.quiz_test_id == test.id))).scalars().all()
    return {a.student_id: a for a in rows}


async def results(session: AsyncSession, test: QuizTest) -> dict:
    """Per student: not started / in progress / done with score and levels;
    the class's average per level, its weakest and who needs help."""
    students = (await session.execute(select(User.id, User.display_name).join(
        StudentProfile, StudentProfile.user_id == User.id).where(StudentProfile.class_id == test.class_id)
        .order_by(User.display_name))).all()
    tried = await attempts(session, test)
    rows, levels = [], {}
    for sid, name in students:
        a = tried.get(sid)
        status = "not_started" if a is None else "done" if a.outcome else "in_progress"
        rows.append({"student_id": str(sid), "name": name, "status": status,
                     "score_percent": a.score_percent if a and a.outcome else None,
                     "per_bloom": topic_quiz.ordered(a.per_bloom) if a and a.outcome else None,
                     "lagging": (a.analysis or {}).get("lagging", []) if a and a.outcome else []})
        for level, pct in ((a.per_bloom or {}) if a and a.outcome else {}).items():
            levels.setdefault(level, []).append(pct)
    averages = topic_quiz.ordered({k: round(sum(v) / len(v)) for k, v in levels.items()}) or {}
    weakest = topic_quiz.lagging_levels(averages)
    done = [r for r in rows if r["status"] == "done"]
    return {"student_count": len(rows), "done": len(done),
            "average_percent": round(sum(r["score_percent"] for r in done) / len(done)) if done else None,
            "per_bloom": averages, "weakest_level": weakest[0] if weakest else None,
            "needs_help": [r["name"] for r in done if (r["score_percent"] or 0) < topic_quiz.LAGGING_BELOW],
            "students": rows}
