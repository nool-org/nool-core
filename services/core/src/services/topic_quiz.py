"""Topic quizzes - the learning loop for each topic the teacher marks taught:

    quiz (8 MCQs, 2 per Bloom level, + 2 spoken answers) -> analysis (lagging level, what to
    know, next step) -> homework on the gaps (assigned at once) -> re-check
    quiz (6 MCQs, weighted to the lagging levels) -> mastered, or flagged
    for the teacher. No third round.

A quiz is a DailyPracticeSession with topic_id and attempt (1 = quiz,
2 = re-check), so questions come from daily practice's pool (_fill) and
answers go through its MCQ answering, XP and topic mastery. A topic with
nothing lagging after the first quiz is mastered straight away.

The stage of each topic is derived (loop_of), never stored.
"""

import logging
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    ClassTopicProgress,
    DailyPracticeItem,
    DailyPracticeSession,
    Homework,
    QuizTest,
    HomeworkDifficulty,
    HomeworkQuestion,
    HomeworkStatus,
    HomeworkTargetStudent,
    SchoolClass,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentProfile,
    Subject,
    Topic,
    User,
)
from src.services import ai_context, daily_practice, generation_client, push, question_types, subject_rules
from src.services.daily_practice import Slot

logger = logging.getLogger(__name__)

LEVELS = [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND, BloomLevel.APPLY, BloomLevel.ANALYZE]  # MCQ levels
SCORED = [*LEVELS, BloomLevel.EVALUATE, BloomLevel.CREATE]  # what the analysis can report on
# A mixed subject's (Maths) 6 MCQs: mostly problems to work out (Apply), one recall, one concept check.
PROBLEM_LEVELS = [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND] + [BloomLevel.APPLY] * 4
# Per subject, the question types a quiz adds after its MCQs, each at its level (question_types.py).
# Maths swaps its Analyze MCQs for these - they test reasoning MCQs can't.
QUIZ_MIX: dict[str, list[tuple[str, BloomLevel]]] = {
    "Mathematics": [("who_is_right", BloomLevel.ANALYZE), ("fix_steps", BloomLevel.EVALUATE),
                    ("make_your_own", BloomLevel.CREATE)],
}
PER_LEVEL = 2  # first quiz: 8 questions
RECHECK_SIZE = 6
VOICE_LEVELS = [BloomLevel.UNDERSTAND, BloomLevel.APPLY]  # the 2 spoken answers on a first quiz
LAGGING_BELOW = daily_practice.GOOD_SCORE  # a level under 70% right is lagging
HOMEWORK_QUESTIONS = 5
HOMEWORK_WINDOW_HOURS = 12  # topic homework is due the same day


# ------------------------------------------------------------------ pure parts
def quiz_levels(attempt: int, lagging: list[str] | None = None) -> list[BloomLevel]:
    """The quiz's Bloom level per question. Re-check: two on each lagging
    level (weakest first), then one each on the others, up to 6."""
    if attempt == 1:
        return [level for level in LEVELS for _ in range(PER_LEVEL)]
    weak = [BloomLevel(v) for v in (lagging or [])]
    levels = [level for level in weak for _ in range(PER_LEVEL)]
    levels += [level for level in LEVELS if level not in weak]
    while len(levels) < RECHECK_SIZE:
        levels += weak or LEVELS
    return levels[:RECHECK_SIZE]


def quiz_slots(attempt: int, lagging: list[str] | None = None, subject: str | None = None) -> list[tuple[str, BloomLevel]]:
    """(kind, level) per question: the MCQs, the subject's own question types
    (QUIZ_MIX), then spoken answers - on a re-check, on the weakest levels
    (a spoken answer shows understanding best).

    Without a mix: 8 MCQs (2 per level) + 2 spoken; re-check 6 + 2.
    With one (Maths): 6 MCQs (PROBLEM_LEVELS: 4 Apply problems) + the mix + 1 spoken; on a re-check,
    MCQs on the lagging MCQ levels + the mix types whose level lags (else the
    first) + 1 spoken."""
    mix = QUIZ_MIX.get(subject or "", [])
    weak = [BloomLevel(v) for v in (lagging or [])] if attempt == 2 else []
    if not mix:
        mcqs = [("mcq", level) for level in quiz_levels(attempt, lagging)]
        voice = list(dict.fromkeys(weak + VOICE_LEVELS))[:2]
        return mcqs + [("voice", level) for level in voice]
    mixed_levels = {level for _, level in mix}
    if attempt == 1:
        mcq_levels = list(PROBLEM_LEVELS)
        extra = list(mix)
    else:
        mcq_weak = [v for v in (lagging or []) if BloomLevel(v) in LEVELS and BloomLevel(v) not in mixed_levels]
        mcq_levels = [lv for lv in quiz_levels(2, mcq_weak) if lv not in mixed_levels][:4]
        extra = [(k, lv) for k, lv in mix if lv in weak] or mix[:1]
    voice = list(dict.fromkeys([lv for lv in weak if lv in VOICE_LEVELS] + VOICE_LEVELS))[:1]
    if not subject_rules.voice_tests_allowed(subject):
        voice = []
    return [("mcq", level) for level in mcq_levels] + extra + [("voice", level) for level in voice]


def per_bloom(answers: list[tuple[BloomLevel, bool]]) -> dict[str, int]:
    """Percent right per level, in Bloom order."""
    out = {}
    for level in SCORED:
        marks = [right for lv, right in answers if lv == level]
        if marks:
            out[level.value] = round(100 * sum(marks) / len(marks))
    return out


def ordered(scores: dict | None) -> dict[str, int] | None:
    """Per-level scores in Bloom order - JSONB doesn't keep key order."""
    if scores is None:
        return None
    return {level.value: scores[level.value] for level in SCORED if level.value in scores}


def lagging_levels(scores: dict[str, int]) -> list[str]:
    """Levels under LAGGING_BELOW, weakest first (the lower Bloom level on a tie)."""
    order = [level.value for level in SCORED]
    return sorted((k for k, v in scores.items() if v < LAGGING_BELOW), key=lambda k: (scores[k], order.index(k)))


def outcome_of(attempt: int, lagging: list[str]) -> str:
    if not lagging:
        return "mastered"
    return "homework" if attempt == 1 else "needs_teacher"


def fallback_analysis(topic: str, attempt: int, lagging: list[str]) -> dict:
    """Plain words when the generation service is down."""
    if not lagging:
        return {"summary": f"You have {topic} sorted - well done.", "lagging_meaning": "", "need_to_know": [],
                "next_step": "Move on to the next topic."}
    level = lagging[0].lower()
    return {
        "summary": f"You're getting there with {topic}; the {level} questions tripped you up.",
        "lagging_meaning": f"Questions that ask you to {level} ideas from {topic} need more practice.",
        "need_to_know": [],
        "next_step": ("Do your homework on these gaps, then take the re-check quiz." if attempt == 1
                      else "Go over the revision cards - your teacher will help with the rest."),
    }


# ------------------------------------------------------------------ helpers
async def _student_class(session: AsyncSession, student: User) -> SchoolClass:
    school_class = await daily_practice.class_of(session, student.id)
    if school_class is None:
        raise ConflictError("You aren't in a class yet.")
    return school_class


async def _taught_topic(session: AsyncSession, school_class: SchoolClass, topic_id: str) -> Topic:
    try:
        tid = uuid.UUID(topic_id)
    except ValueError:
        raise NotFoundError("Topic not found.") from None
    topic = (await session.execute(select(Topic).join(
        ClassTopicProgress, ClassTopicProgress.topic_id == Topic.id).where(
        Topic.id == tid, ClassTopicProgress.class_id == school_class.id))).scalar_one_or_none()
    if topic is None:
        raise NotFoundError("That topic hasn't been taught in your class yet.")
    return topic


async def _quizzes(session: AsyncSession, student_ids, topic_ids) -> dict[tuple, DailyPracticeSession]:
    """{(student_id, topic_id, attempt): quiz}"""
    if not student_ids or not topic_ids:
        return {}
    rows = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.student_id.in_(list(student_ids)),
        DailyPracticeSession.topic_id.in_(list(topic_ids))))).scalars().all()
    return {(q.student_id, q.topic_id, q.attempt): q for q in rows}


async def _homework_status(session: AsyncSession, quizzes) -> dict[uuid.UUID, StudentHomeworkStatus]:
    """{homework_id: the student's status on it}"""
    pairs = [(q.homework_id, q.student_id) for q in quizzes if q.homework_id]
    if not pairs:
        return {}
    rows = (await session.execute(select(StudentHomeworkProgress).where(
        StudentHomeworkProgress.homework_id.in_([h for h, _ in pairs])))).scalars().all()
    wanted = set(pairs)
    return {r.homework_id: r.status for r in rows if (r.homework_id, r.student_id) in wanted}


@dataclass
class Loop:
    topic: Topic
    taught_on: object
    stage: str  # quiz | homework_pending | homework | recheck | mastered | needs_teacher
    quiz: DailyPracticeSession | None
    recheck: DailyPracticeSession | None


def stage_of(quiz, recheck, homework_status) -> str:
    if quiz is None or quiz.outcome is None:
        return "quiz"
    if quiz.outcome == "mastered":
        return "mastered"
    if recheck is not None and recheck.outcome is not None:
        return recheck.outcome
    if quiz.homework_id is None and not (quiz.analysis or {}).get("homework_skipped"):
        return "homework_pending"
    if quiz.homework_id is not None and homework_status != StudentHomeworkStatus.COMPLETED:
        return "homework"
    return "recheck"


async def loops_for(session: AsyncSession, student_ids, class_id, topic_ids=None) -> dict[uuid.UUID, list[Loop]]:
    """Each student's loop on every topic taught in the class (newest taught first)."""
    query = (select(ClassTopicProgress, Topic).join(Topic, Topic.id == ClassTopicProgress.topic_id)
             .where(ClassTopicProgress.class_id == class_id)
             .order_by(ClassTopicProgress.taught_on.desc(), ClassTopicProgress.created_at.desc()))
    if topic_ids is not None:
        query = query.where(Topic.id.in_(list(topic_ids)))
    taught = (await session.execute(query)).all()
    quizzes = await _quizzes(session, student_ids, [t.id for _, t in taught])
    statuses = await _homework_status(session, quizzes.values())
    out = {}
    for sid in student_ids:
        loops = []
        for progress, topic in taught:
            quiz, recheck = quizzes.get((sid, topic.id, 1)), quizzes.get((sid, topic.id, 2))
            status = statuses.get(quiz.homework_id) if quiz and quiz.homework_id else None
            loops.append(Loop(topic, progress.taught_on, stage_of(quiz, recheck, status), quiz, recheck))
        out[sid] = loops
    return out


async def loop_of(session: AsyncSession, student: User, chapter_id=None, subject_id=None) -> list[Loop]:
    school_class = await daily_practice.class_of(session, student.id)
    if school_class is None:
        return []
    topic_ids = None
    if chapter_id is not None:
        topic_ids = (await session.execute(select(Topic.id).where(Topic.chapter_id == chapter_id))).scalars().all()
    elif subject_id is not None:
        topic_ids = (await session.execute(select(Topic.id).join(Chapter, Chapter.id == Topic.chapter_id).where(
            Chapter.subject_id == subject_id))).scalars().all()
    return (await loops_for(session, [student.id], school_class.id, topic_ids))[student.id]


# ------------------------------------------------------------------ quiz
async def start(session: AsyncSession, student: User, topic_id: str, attempt: int) -> DailyPracticeSession:
    """The student's quiz (attempt 1) or re-check (attempt 2) on a taught
    topic - built on first call, the same one after. A re-check only once
    the homework is done. 409 when it isn't open yet. Caller commits."""
    school_class = await _student_class(session, student)
    topic = await _taught_topic(session, school_class, topic_id)
    loop = (await loops_for(session, [student.id], school_class.id, [topic.id]))[student.id][0]
    existing = loop.quiz if attempt == 1 else loop.recheck
    if existing is not None:
        return existing
    if attempt == 2 and loop.stage != "recheck":
        raise ConflictError("Finish the homework first - then the re-check quiz opens.")
    lagging = (loop.quiz.analysis or {}).get("lagging", []) if attempt == 2 else None
    created = (await session.execute(pg_insert(DailyPracticeSession).values(
        id=uuid.uuid4(), student_id=student.id, practice_date=daily_practice.today_ist(),
        chapter_id=topic.chapter_id, topic_id=topic.id, attempt=attempt, status="open", xp_awarded=0,
    ).on_conflict_do_nothing(index_elements=["student_id", "topic_id", "attempt"],
                             index_where=DailyPracticeSession.topic_id.is_not(None))
        .returning(DailyPracticeSession.id))).scalar_one_or_none()
    quiz = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.student_id == student.id, DailyPracticeSession.topic_id == topic.id,
        DailyPracticeSession.attempt == attempt).execution_options(populate_existing=True))).scalar_one()
    if created is None:  # a concurrent first open built it
        return quiz
    chapter = await session.get(Chapter, topic.chapter_id)
    subject = await session.get(Subject, chapter.subject_id) if chapter else None
    slots = [Slot(topic, kind, level) for kind, level in quiz_slots(attempt, lagging, subject.name if subject else None)]
    # ponytail: holds the row through the LLM calls on a cold pool, as daily practice does
    with ai_context.feature("topic_quiz_mcq"):
        chosen = await daily_practice._fill(session, student, school_class, slots, daily_practice.today_ist(),
                                            str(quiz.id), exact_level=True)
    if not chosen:
        await session.delete(quiz)
        await session.flush()
        raise ConflictError("No questions are ready for this topic yet - try again in a little while.")
    session.add_all([DailyPracticeItem(session_id=quiz.id, order=order, practice_question_id=q.id, kind=q.kind,
                                       topic_id=topic.id, bloom_level=q.bloom_level)
                     for order, (slot, q) in enumerate(chosen, start=1)])
    await session.flush()
    return quiz


async def own_quiz(session: AsyncSession, quiz_id: str, student: User) -> DailyPracticeSession:
    quiz = await daily_practice.own_session(session, quiz_id, student)
    if quiz.topic_id is None and quiz.quiz_test_id is None:  # the day's practice isn't a quiz
        raise NotFoundError("Quiz not found.")
    return quiz


def focus_topic_id(items) -> uuid.UUID | None:
    """A multi-topic test's weakest topic (most of its questions missed;
    ties to the earlier one) - its homework is made on it."""
    tally: dict = {}
    for item, _, _ in items:
        if item.topic_id is not None:
            right, total = tally.get(item.topic_id, (0, 0))
            tally[item.topic_id] = (right + bool(item.correct), total + 1)
    if not tally:
        return None
    return min(tally, key=lambda t: tally[t][0] / tally[t][1])


async def _analysis(session: AsyncSession, quiz: DailyPracticeSession, topic: Topic, scores: dict, lagging: list,
                    items, label: str | None = None) -> dict:
    chapter = await session.get(Chapter, topic.chapter_id)
    subject = await session.get(Subject, chapter.subject_id) if chapter else None
    school_class = await daily_practice.class_of(session, quiz.student_id)
    first = None
    if quiz.attempt == 2:
        first = (await session.execute(select(DailyPracticeSession.per_bloom).where(
            DailyPracticeSession.student_id == quiz.student_id, DailyPracticeSession.topic_id == topic.id,
            DailyPracticeSession.attempt == 1))).scalar_one_or_none()
    missed = [{"bloom_level": item.bloom_level.value, "question": q.text,
               "chosen": q.options[item.option_index] if item.option_index is not None else "",
               "correct": q.options[q.correct_index]}
              for item, q, _ in items if not item.correct and q.options and q.correct_index is not None]
    missed += [{"bloom_level": item.bloom_level.value, "question": f"(answered aloud) {q.text}",
                "chosen": (item.transcript or "")[:500], "correct": "; ".join(q.expected_points or [])[:500]}
               for item, q, _ in items if item.kind == "voice" and item.answered_at and not item.correct]
    missed += [{"bloom_level": item.bloom_level.value, "question": f"({item.kind.replace('_', ' ')}) {q.text}",
                "chosen": f"{(item.feedback or {}).get('message', '')} [mistake: {(item.feedback or {}).get('mistake')}]"[:500],
                "correct": (q.spec or {}).get("explanation", "")[:500]}
               for item, q, _ in items if item.kind in question_types.KINDS and item.answered_at and not item.correct]
    name = label or daily_practice.tidy(topic.name)
    try:
        with ai_context.feature("topic_quiz_analysis"):
            words = await generation_client.quiz_analysis({
                "grade": school_class.grade if school_class else 10,
                "subject": subject.name if subject else "Science",
                "chapter": chapter.name if chapter else name, "topic": name, "attempt": quiz.attempt,
                "per_bloom": scores, "previous_per_bloom": first, "lagging_level": lagging[0] if lagging else None,
                "missed": missed[:20]})
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("topic_quiz_analysis_unavailable", extra={"quiz_id": str(quiz.id)})
        words = fallback_analysis(name, quiz.attempt, lagging)
    return {**{k: words.get(k) for k in ("summary", "lagging_meaning", "need_to_know", "next_step")},
            "lagging": lagging, "previous_per_bloom": first}


async def finish(session: AsyncSession, quiz: DailyPracticeSession) -> dict:
    """Marks a quiz with every MCQ answered (409 before): XP via daily practice's
    completion, then per-level scores, the analysis and the outcome - once;
    calling again returns the stored result. Caller commits, then queues
    build_homework when the outcome is "homework"."""
    # spoken answers can be skipped (web, no microphone) - they just don't count
    completion = await daily_practice.complete(session, quiz, skippable_voice=True)
    if quiz.outcome is None:
        items = [row for row in await daily_practice.items_of(session, quiz.id) if row[0].answered_at is not None]
        scores = per_bloom([(item.bloom_level, bool(item.correct)) for item, _, _ in items])
        lagging = lagging_levels(scores)
        label = None
        if quiz.topic_id is not None:
            topic_id = quiz.topic_id
        else:  # a teacher's test: its homework goes on its weakest topic
            test = await session.get(QuizTest, quiz.quiz_test_id)
            topic_id = focus_topic_id(items) or uuid.UUID(str(test.topic_ids[0]))
            label = test.title
        topic = await session.get(Topic, topic_id)
        quiz.per_bloom = scores
        quiz.score_percent = round(100 * sum(bool(i.correct) for i, _, _ in items) / len(items))
        quiz.analysis = {**await _analysis(session, quiz, topic, scores, lagging, items, label),
                         "focus_topic_id": str(topic.id)}
        quiz.outcome = outcome_of(quiz.attempt, lagging)
        await session.flush()
    return {"quiz": quiz, "xp_awarded": quiz.xp_awarded, "streak_days": completion["streak_days"]}


# ------------------------------------------------------------------ homework
async def build_homework(quiz_id: uuid.UUID) -> None:
    """The personal homework a first quiz with lagging levels leads to: 5
    questions on the topic, spread over the lagging levels, assigned at once
    and pushed. Own session; logged, never raised. With no dataset to ground
    it in, the homework is skipped and the re-check opens straight away."""
    from src.repositories import get_session
    from src.services.homework_builder import Focus, build_homework_questions, grounding_dataset_id

    try:
        async with get_session() as session:
            quiz = (await session.execute(select(DailyPracticeSession).where(
                DailyPracticeSession.id == quiz_id).with_for_update())).scalar_one_or_none()
            if quiz is None or quiz.outcome != "homework" or quiz.homework_id is not None:
                return
            student = await session.get(User, quiz.student_id)
            topic = await session.get(Topic, quiz.topic_id or uuid.UUID((quiz.analysis or {})["focus_topic_id"]))
            chapter = await session.get(Chapter, topic.chapter_id)
            profile = (await session.execute(select(StudentProfile).where(
                StudentProfile.user_id == student.id))).scalar_one_or_none()
            # what the homework builder needs of a "test": its id, class, subject, chapter and topic
            scope = SimpleNamespace(id=quiz.id, class_id=profile.class_id if profile else None,
                                    subject_id=chapter.subject_id, chapter_id=chapter.id, topic_id=topic.id)
            dataset_id = await grounding_dataset_id(session, user=student, test=scope) if scope.class_id else None
            if dataset_id is None:
                quiz.analysis = {**(quiz.analysis or {}), "homework_skipped": True}
                await session.commit()
                return
            levels = [BloomLevel(v) for v in (quiz.analysis or {}).get("lagging", [])] or [BloomLevel.UNDERSTAND]
            weights = {level: HOMEWORK_QUESTIONS // len(levels) for level in levels}
            weights[levels[0]] += HOMEWORK_QUESTIONS - sum(weights.values())
            hw = Homework(test_id=None, topic_id=topic.id, class_id=scope.class_id,
                          gap_topic=daily_practice.tidy(topic.name), gap_mastery_percent=quiz.score_percent or 0,
                          total_questions=HOMEWORK_QUESTIONS, difficulty=HomeworkDifficulty.MIXED,
                          target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS,
                          completion_window_hours=HOMEWORK_WINDOW_HOURS, status=HomeworkStatus.ASSIGNED,
                          assigned_count=1)
            session.add(hw)
            await session.flush()
            session.add(HomeworkTargetStudent(homework_id=hw.id, student_id=student.id))
            session.add(StudentHomeworkProgress(homework_id=hw.id, student_id=student.id))
            with ai_context.feature("topic_quiz_homework"):
                drafts = await build_homework_questions(
                    session, user=student, test=scope, total=HOMEWORK_QUESTIONS, bloom_weights=weights,
                    difficulty=HomeworkDifficulty.MIXED, fallback_dataset_id=dataset_id, seed=str(hw.id),
                    student_id=student.id, focus=Focus(topic_id=topic.id))
            for order, d in enumerate(drafts, start=1):
                session.add(HomeworkQuestion(homework_id=hw.id, order=order, bloom_level=d.bloom_level,
                                             dataset_id=d.dataset_id, source_question_id=d.source_question_id,
                                             text=d.text, answer=d.answer,
                                             **({"is_boss": True} if d.is_boss else {})))
            quiz.homework_id = hw.id
            await session.commit()
            await push.notify(session, [student.id], f"Homework ready: {hw.gap_topic}",
                              "Made for the gaps in your quiz.", {"type": "homework", "homeworkId": str(hw.id)})
    except Exception:  # logged, never raised: the quiz result already stands
        logger.exception("topic_quiz_homework_failed", extra={"quiz_id": str(quiz_id)})


# ------------------------------------------------------------------ co-learner
FOCUS_MAX = 600  # colearner's SessionConfig.student_focus


async def chapter_focus(session: AsyncSession, student: User, chapter_id) -> str | None:
    """One line for Milo on how the student did on the chapter's topic
    quizzes - where they found a thinking skill hard (and whether it still is
    after homework) and what they've mastered. None before any quiz."""
    if chapter_id is None:
        return None
    hard, mastered = [], []
    for loop in await loop_of(session, student, chapter_id):
        latest = loop.recheck if loop.recheck and loop.recheck.outcome else loop.quiz
        if latest is None or latest.outcome is None:
            continue
        name = daily_practice.tidy(loop.topic.name)
        if loop.stage == "mastered":
            mastered.append(name)
            continue
        levels = " and ".join(level.title() for level in (latest.analysis or {}).get("lagging", [])[:2]) or "it"
        note = {"needs_teacher": " (still hard after homework)", "recheck": " (homework done)"}.get(loop.stage, "")
        hard.append(f"found {levels} hard on {name}{note}")
    parts = []
    if hard:
        line = "; ".join(hard)
        parts.append(line[0].upper() + line[1:] + ".")
    if mastered:
        parts.append(f"Has mastered: {', '.join(mastered)}.")
    return " ".join(parts)[:FOCUS_MAX] or None


# ------------------------------------------------------------------ teacher
async def class_topic_summary(session: AsyncSession, class_id: uuid.UUID, chapter_id: uuid.UUID) -> list[dict]:
    """Per taught topic of the chapter: how many students quizzed, their
    average, the class's weakest Bloom level and who needs the teacher."""
    students = (await session.execute(select(User.id, User.display_name).join(
        StudentProfile, StudentProfile.user_id == User.id).where(StudentProfile.class_id == class_id))).all()
    names = dict(students)
    topic_ids = (await session.execute(select(Topic.id).where(Topic.chapter_id == chapter_id))).scalars().all()
    loops = await loops_for(session, list(names), class_id, topic_ids)
    out = {}
    for sid, student_loops in loops.items():
        for loop in student_loops:
            row = out.setdefault(loop.topic.id, {"topic_id": str(loop.topic.id), "topic_name": loop.topic.name,
                                                 "student_count": len(names), "quizzed": 0, "mastered": 0,
                                                 "scores": [], "bloom": {}, "needs_teacher": []})
            if loop.quiz is not None and loop.quiz.outcome is not None:
                row["quizzed"] += 1
                row["scores"].append(loop.quiz.score_percent or 0)
                for level, pct in (loop.quiz.per_bloom or {}).items():
                    row["bloom"].setdefault(level, []).append(pct)
            if loop.stage == "mastered":
                row["mastered"] += 1
            if loop.stage == "needs_teacher":
                lagging = (loop.recheck.analysis or {}).get("lagging", [])
                row["needs_teacher"].append({"student_id": str(sid), "name": names[sid],
                                             "lagging": lagging[0] if lagging else None})
    result = []
    for row in out.values():
        averages = {k: round(sum(v) / len(v)) for k, v in row.pop("bloom").items()}
        scores = row.pop("scores")
        lagging = lagging_levels(averages)
        result.append({**row, "average_percent": round(sum(scores) / len(scores)) if scores else None,
                       "per_bloom": ordered(averages), "weakest_level": lagging[0] if lagging else None})
    return result

