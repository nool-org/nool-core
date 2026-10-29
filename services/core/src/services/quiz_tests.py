"""Fix Milo's homework, the written challenge a teacher sets in any subject
(subject_rules.challenge_formats): the teacher picks a
chapter's topics and how many problems; the questions are chosen once - the
same for every student - from the shared practice pool (bank, then
generation; daily_practice._fill), reviewed, then published to the class.

A student's attempt is a practice session with quiz_test_id and no topic:
answering, the analysis and the homework on its weakest topic are the topic
quiz's (topic_quiz.finish / build_homework). Then, like a check-in, a second
look in the same format (attempt 2: fresh problems on the topics they
missed) - mastered, or their teacher is told.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
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
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentProfile,
    Subject,
    Topic,
    User,
)
from src.services import ai_context, challenge_rules, daily_practice, push, question_types, subject_rules, topic_quiz
from src.services.daily_practice import Slot

logger = logging.getLogger(__name__)

KINDS = ("mcq", *question_types.KINDS)
# the MCQs' levels in turn: mostly problems to work out (Apply), like a board paper
MCQ_LEVELS = [BloomLevel.APPLY, BloomLevel.APPLY, BloomLevel.UNDERSTAND, BloomLevel.APPLY, BloomLevel.ANALYZE,
              BloomLevel.REMEMBER]
KIND_LEVEL = {"who_is_right": BloomLevel.ANALYZE, "fix_steps": BloomLevel.EVALUATE,
              "make_your_own": BloomLevel.CREATE}
MAX_QUESTIONS = 20
SECOND_LOOK = 5  # problems in the second look


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
                 mix: dict[str, int], title: str | None, due_at: datetime | None,
                 completion_window_hours: int | None = None, student_ids: list[str] | None = None) -> QuizTest:
    """The test, 'preparing' - prepare() picks its questions. Caller commits, then queues prepare."""
    from src.api.routes.daily_practice import _get, _teaches

    chapter = await _get(session, Chapter, chapter_id, "Chapter")
    class_uuid = await _teaches(session, teacher, class_id, chapter.subject_id)
    topics = [await _get(session, Topic, t, "Topic") for t in topic_ids]
    if any(t.chapter_id != chapter.id for t in topics):
        raise ValidationError("Every topic must be from the chosen chapter.")
    plan(topics, mix)  # validates the mix
    subject = await session.get(Subject, chapter.subject_id)
    unfit = sorted(k for k, n in mix.items() if int(n) > 0 and k not in subject_rules.challenge_formats(subject.name))
    if unfit:
        raise ValidationError(f"{subject.name} doesn't use: {', '.join(unfit)}.")
    await challenge_rules.ensure_one_per_chapter(session, class_uuid, chapter.id, "fix_steps")
    # a double tap (V-015): one request at a time per teacher + class, and the same quiz twice within a minute is one
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"quiz:{teacher.id}:{class_uuid}", 0))))
    wanted = ([str(t.id) for t in topics], {k: int(v) for k, v in mix.items() if int(v) > 0})
    recent = (await session.execute(select(QuizTest.topic_ids, QuizTest.mix).where(
        QuizTest.created_by == teacher.id, QuizTest.class_id == class_uuid, QuizTest.chapter_id == chapter.id,
        QuizTest.created_at > func.now() - timedelta(minutes=1)))).all()
    if any((list(t), dict(m)) == wanted for t, m in recent):
        raise ConflictError("You just set this up - open it from the list instead of creating it again.")
    test = QuizTest(id=uuid.uuid4(), class_id=class_uuid, subject_id=chapter.subject_id, chapter_id=chapter.id,
                    # a default name a teacher can tell apart from last week's: "Real Numbers test · 13 Oct"
                    title=(title or "").strip() or f"{daily_practice.tidy(chapter.name)} challenge · "
                    f"{daily_practice.today_ist().strftime('%d %b').lstrip('0')}",
                    topic_ids=[str(t.id) for t in topics], mix={k: int(v) for k, v in mix.items() if int(v) > 0},
                    question_ids=[], status="preparing", due_at=due_at, created_by=teacher.id,
                    completion_window_hours=completion_window_hours,
                    student_ids=await _class_students(session, class_uuid, student_ids))
    session.add(test)
    await session.flush()
    return test


PREPARE_TIMEOUT_SECONDS = 300  # past this the teacher sees 'failed' and can set it again
STALE_AFTER = timedelta(minutes=10)  # a 'preparing' this old lost its task (a restart) - it's failed


async def prepare(test_id: uuid.UUID) -> None:
    """Picks the test's questions in its own session: pool first, then the bank,
    then generation (a type that can't be made becomes an MCQ on that level).
    'ready' with questions, else 'failed' - also on an error or after
    PREPARE_TIMEOUT_SECONDS, so it never stays 'preparing'. Logged, never raised."""
    try:
        await asyncio.wait_for(_prepare(test_id), PREPARE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - TimeoutError included
        logger.exception("quiz_test_prepare_failed", extra={"quiz_test_id": str(test_id)})
        from src.repositories import get_session

        try:
            async with get_session() as session:
                await session.execute(update(QuizTest).where(
                    QuizTest.id == test_id, QuizTest.status == "preparing").values(status="failed"))
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("quiz_test_mark_failed_failed", extra={"quiz_test_id": str(test_id)})


async def _prepare(test_id: uuid.UUID) -> None:
    from src.repositories import get_session

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


def status_of(test: QuizTest, now: datetime | None = None) -> str:
    """Its status, with a 'preparing' whose background task died (a restart
    mid-prepare) shown as 'failed' once it can no longer finish."""
    now = now or datetime.now(timezone.utc)
    if test.status == "preparing" and test.created_at and now - test.created_at > STALE_AFTER:
        return "failed"
    return test.status


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
    if test.status == "ready":
        await challenge_rules.ensure_one_per_chapter(session, test.class_id, test.chapter_id, "fix_steps", ignore_id=test.id)
    now = datetime.now(timezone.utc)
    later = opens_at is not None and opens_at > now
    test.status, test.published_at = ("scheduled", opens_at) if later else ("published", now)
    if test.completion_window_hours:  # like a voice co-learner: the window starts when it opens
        test.due_at = test.published_at + timedelta(hours=test.completion_window_hours)
    await session.flush()
    return not later


def is_open(test: QuizTest, now: datetime | None = None) -> bool:
    """Published, or scheduled and its time has come."""
    now = now or datetime.now(timezone.utc)
    return test.status in ("published", "scheduled") and test.published_at is not None and test.published_at <= now


async def _class_students(session: AsyncSession, class_id: uuid.UUID, student_ids: list[str] | None) -> list[str] | None:
    """The chosen students, checked against the class roster (None = the whole class)."""
    if not student_ids:
        return None
    roster = {str(sid) for sid in (await session.execute(select(StudentProfile.user_id).where(
        StudentProfile.class_id == class_id))).scalars()}
    chosen = [sid for sid in dict.fromkeys(student_ids) if sid in roster]
    if not chosen:
        raise ValidationError("Choose students from this class.")
    return chosen


def is_for(test: QuizTest, student_id) -> bool:
    return test.student_ids is None or str(student_id) in test.student_ids


async def notify_class(session: AsyncSession, test: QuizTest) -> None:
    students = [s for s in (await session.execute(select(StudentProfile.user_id).where(
        StudentProfile.class_id == test.class_id))).scalars().all() if is_for(test, s)]
    await push.notify(session, students, f"New challenge: {test.title}", "Your teacher set you a challenge - start it now.",
                      {"type": "quiz_test", "quizTestId": str(test.id)})


MAX_WINDOW_HOURS = 14 * 24


async def remind(session: AsyncSession, test: QuizTest, student_ids: list[str] | None) -> int:
    """Pushes the students who haven't finished (or just the picked ones among them); how many."""
    if not is_open(test) or (test.due_at and test.due_at <= datetime.now(timezone.utc)):
        raise ConflictError("This test isn't open.")
    done = {sid for sid, a in (await attempts(session, test)).items() if a.outcome}
    pending = {uuid.UUID(str(s)) for s in (await session.execute(select(StudentProfile.user_id).where(
        StudentProfile.class_id == test.class_id))).scalars() if is_for(test, s)} - done
    if student_ids is not None:
        pending &= {uuid.UUID(s) for s in student_ids if len(s) == 36}  # a malformed id raises 422-ish below
    if not pending:
        raise ValidationError("Everyone you picked has already finished.")
    left = test.due_at - datetime.now(timezone.utc) if test.due_at else None
    hours = max(1, int(left.total_seconds() // 3600)) if left else None
    await push.notify(session, pending, f"Reminder: {test.title}",
                      f"Your teacher is waiting for your answers{f' - {hours} h left' if hours else ''}.",
                      {"type": "quiz_test", "quizTestId": str(test.id)})
    return len(pending)


def extend(test: QuizTest) -> None:
    """24 more hours for everyone; also reopens one that just closed. Caller commits."""
    if test.status not in ("published", "scheduled") or test.published_at is None:
        raise ConflictError("Open the test first.")
    hours = (test.completion_window_hours or 0) + 24
    if hours > MAX_WINDOW_HOURS:
        raise ValidationError("A test can stay open for at most 14 days.")
    test.completion_window_hours = hours
    test.due_at = max(test.due_at or test.published_at, datetime.now(timezone.utc)) + timedelta(hours=24)


# ------------------------------------------------------------------ students
async def tests_for(session: AsyncSession, student: User) -> list[QuizTest]:
    profile = (await session.execute(select(StudentProfile).where(
        StudentProfile.user_id == student.id))).scalar_one_or_none()
    if profile is None:
        return []
    return [t for t in (await session.execute(select(QuizTest).where(
        QuizTest.class_id == profile.class_id, QuizTest.status.in_(("published", "scheduled")),
        QuizTest.published_at <= datetime.now(timezone.utc))  # the app's clock, as publish() stamps it
        .order_by(QuizTest.published_at.desc()))).scalars().all() if is_for(t, student.id)]


async def _in_class(session: AsyncSession, student: User, test: QuizTest) -> bool:
    class_id = (await session.execute(select(StudentProfile.class_id).where(
        StudentProfile.user_id == student.id))).scalar_one_or_none()
    return class_id == test.class_id and is_for(test, student.id)


async def _attempt(session: AsyncSession, student: User, test: QuizTest, attempt: int) -> DailyPracticeSession | None:
    return (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.student_id == student.id, DailyPracticeSession.quiz_test_id == test.id,
        DailyPracticeSession.attempt == attempt).execution_options(populate_existing=True))).scalar_one_or_none()


async def second_look_open(session: AsyncSession, first: DailyPracticeSession | None) -> bool:
    """After a first attempt that led to homework, once that homework is done
    (or there was none to make)."""
    if first is None or first.outcome != "homework":
        return False
    if (first.analysis or {}).get("homework_skipped"):
        return True
    if first.homework_id is None:
        return False
    status = (await session.execute(select(StudentHomeworkProgress.status).where(
        StudentHomeworkProgress.homework_id == first.homework_id,
        StudentHomeworkProgress.student_id == first.student_id))).scalar_one_or_none()
    return status == StudentHomeworkStatus.COMPLETED


def second_look_plan(test: QuizTest, first_items, topics: list[Topic]) -> list[Slot]:
    """SECOND_LOOK questions in the test's own formats, on the topics the
    student missed (all the test's topics if none)."""
    missed = list(dict.fromkeys(item.topic_id for item, _, _ in first_items if not item.correct and item.topic_id))
    focus = [t for t in topics if t.id in missed] or topics
    kinds = [k for k in KINDS if (test.mix or {}).get(k)] or ["mcq"]
    return plan(focus, {k: SECOND_LOOK // len(kinds) + (n < SECOND_LOOK % len(kinds)) for n, k in enumerate(kinds)})


async def start(session: AsyncSession, student: User, test_id: str, attempt: int = 1) -> DailyPracticeSession:
    """The student's attempt (1) or second look (2, once the homework is done:
    fresh questions, same formats) - built on first call, the same one after.
    Caller commits."""
    try:
        tid = uuid.UUID(test_id)
    except ValueError:
        raise NotFoundError("Test not found.") from None
    test = await session.get(QuizTest, tid)
    mine = test is not None and test in await tests_for(session, student)
    if test is not None and test.status == "scheduled" and not is_open(test) and await _in_class(session, student, test):
        opens = test.published_at.astimezone(ZoneInfo("Asia/Kolkata"))  # V-007: say when, not "not found"
        raise ConflictError(f"This test isn't open yet. It opens {opens.day} {opens:%b}, "
                            f"{opens.hour % 12 or 12}:{opens:%M} {'am' if opens.hour < 12 else 'pm'}.", code="NOT_OPEN_YET")
    if test is None or not is_open(test) or not mine:
        raise NotFoundError("Test not found.")
    if attempt == 2 and await _attempt(session, student, test, 2) is None:
        if not await second_look_open(session, await _attempt(session, student, test, 1)):
            raise ConflictError("Finish your homework first - then the re-check opens.")
    created = (await session.execute(pg_insert(DailyPracticeSession).values(
        id=uuid.uuid4(), student_id=student.id, practice_date=daily_practice.today_ist(),
        chapter_id=test.chapter_id, quiz_test_id=test.id, attempt=attempt, status="open", xp_awarded=0,
    ).on_conflict_do_nothing(index_elements=["student_id", "quiz_test_id", "attempt"],
                             index_where=DailyPracticeSession.quiz_test_id.is_not(None))
        .returning(DailyPracticeSession.id))).scalar_one_or_none()
    current = await _attempt(session, student, test, attempt)
    if created is None:
        return current
    if attempt == 1:
        questions = [(q.topic_id, q) for q in await questions_of(session, test)]
    else:
        first = await _attempt(session, student, test, 1)
        topics = [await session.get(Topic, uuid.UUID(str(t))) for t in test.topic_ids]
        school_class = await session.get(SchoolClass, test.class_id)
        slots = second_look_plan(test, await daily_practice.items_of(session, first.id), topics)
        # ponytail: holds the row through the LLM calls on a cold pool, as the check-in's re-check does
        with ai_context.feature("quiz_test_second_look"):
            chosen = await daily_practice._fill(session, student, school_class, slots, daily_practice.today_ist(),
                                                str(current.id), exact_level=True)
        if not chosen:
            await session.delete(current)
            await session.flush()
            raise ConflictError("No new questions are ready yet - try again in a little while.")
        questions = [(slot.topic.id, q) for slot, q in chosen]
    session.add_all([DailyPracticeItem(session_id=current.id, order=n, practice_question_id=q.id, kind=q.kind,
                                       topic_id=topic_id, bloom_level=q.bloom_level)
                     for n, (topic_id, q) in enumerate(questions, start=1)])
    await session.flush()
    return current


async def attempts(session: AsyncSession, test: QuizTest) -> dict[uuid.UUID, DailyPracticeSession]:
    rows = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.quiz_test_id == test.id, DailyPracticeSession.attempt == 1))).scalars().all()
    return {a.student_id: a for a in rows}  # the first attempt is the score


async def results(session: AsyncSession, test: QuizTest) -> dict:
    """Per student: not started / in progress / done with score and levels;
    the class's average per level, its weakest and who needs help."""
    students = (await session.execute(select(User.id, User.display_name).join(
        StudentProfile, StudentProfile.user_id == User.id).where(StudentProfile.class_id == test.class_id)
        .order_by(User.display_name))).all()
    students = [row for row in students if is_for(test, row[0])]  # only the students it was set for
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
