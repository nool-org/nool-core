"""The follow-up MCQ test after a voice test - closes the loop from "where
you lag" to "did it improve".

- Unlock: one McqTest per (student, voice test result), created lazily when
  the student lists their MCQ tests (sync_for_student). It unlocks once the
  student has confirmed that test's homework, or 48h after the voice test,
  whichever comes first.
- Questions (ensure_questions; prebuild() runs it as soon as homework is
  confirmed): 10 = 2 per Bloom level of the voice test, on the student's
  weakest voice-test concepts plus up to 2 strong ones as a check. Written by
  the generation service first (distractors from the voice test's
  misconceptions); slots it can't fill take a bank MCQ of the chapter on the
  same level and concept, then any unused bank MCQ of the chapter.
  The last question is the boss (Analyze or harder, bonus XP); each carries
  a one-line explanation and an "Ask Milo" hint that never gives the answer.
- Play (record_answer, use_lifeline, set_speed_mode): the FIRST answer to
  each question is kept and marked at once (McqQuestionAnswer); lifelines
  50:50 and Ask Milo once each per test.
- Attempt (submit_attempt): scored from those first answers, one attempt
  only; growth math in services/growth.py. XP (xp_breakdown) adds combo,
  boss and speed bonuses, but score, mastery and growth stay correctness
  only.
"""

import logging
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    BloomLevel,
    Chapter,
    DatasetQuestion,
    DatasetType,
    Homework,
    McqQuestionAnswer,
    McqTest,
    McqTestQuestion,
    QuestionType,
    SchoolClass,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentPoints,
    StudentProfile,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    Topic,
    User,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestQuestion,
)
from src.repositories.dataset_access import visible_datasets
from src.repositories.database import get_session
from src.services import generation_client, push
from src.services.growth import corrected, growth_entries, percent
from src.services.homework_builder import _sections, question_uid
from src.services.question_selector import text_only_filter

logger = logging.getLogger(__name__)

MCQ_QUESTION_COUNT = 10
UNLOCK_AFTER = timedelta(hours=48)
STRONG_PERCENT = 75
MAX_STRONG_CHECKS = 2
XP_PER_CORRECT = 10
BOSS_BONUS = 10
COMBO_FROM, COMBO_BONUS = 3, 5  # +5 for the 3rd consecutive correct answer and each one after
SPEED_LIMIT_MS, SPEED_BONUS = 15_000, 3
LIFELINE_COST = 5
LIFELINES = ("fifty_fifty", "ask_milo")
CLASS_PERCENTILE_MIN = 3  # classmates' completed checks needed for "you beat X%"
_BLOOM_ORDER = list(BloomLevel)


def status(mcq: McqTest) -> str:
    if mcq.completed_at is not None:
        return "completed"
    return "available" if mcq.unlocked_at is not None else "locked"


_INLINE_OPTION = re.compile(r"^\s*\(?[a-dA-D][.)]\s+\S")


def strip_inline_options(text: str, options: list[str] | None = None) -> str:
    """Bank questions sometimes carry their choices in the stem ("a. -20 cm").
    The options are shown as buttons, so drop those trailing lines. Given the
    options, only lines that are options (and blank lines between them) - a
    stem's own lettered list ("A. Riya adds water...") stays."""
    lines = text.rstrip().split("\n")
    opts = {unlabel(o).lower() for o in options or []}
    while lines and (_INLINE_OPTION.match(lines[-1]) or (opts and not lines[-1].strip())):
        if opts and lines[-1].strip() and unlabel(lines[-1]).lower() not in opts:
            break
        lines.pop()
    return "\n".join(lines).rstrip() or text


# ------------------------------------------------------------------ unlock
async def sync_for_student(session: AsyncSession, student_id: uuid.UUID) -> list[McqTest]:
    """Creates the missing McqTest rows for the student's voice test results
    and unlocks the due ones. Newest first. Caller commits - except that an
    unlock commits right here, so its push goes out only once it stands. The
    unlock is a guarded UPDATE, so each MCQ test unlocks (and pushes) once,
    even under concurrent requests."""
    test_ids = (await session.execute(
        select(StudentTestResult.test_id).where(StudentTestResult.student_id == student_id))).scalars().all()
    have = set((await session.execute(
        select(McqTest.voice_test_id).where(McqTest.student_id == student_id))).scalars().all())
    now = datetime.now(timezone.utc)
    for test_id in set(test_ids) - have:
        # ponytail: the session's start stands in for "result time" (StudentTestResult has no timestamp)
        taken_at = (await session.execute(select(func.max(AiAssessorSession.created_at)).where(
            AiAssessorSession.student_id == student_id, AiAssessorSession.test_id == test_id))).scalar_one()
        await session.execute(pg_insert(McqTest).values(
            id=uuid.uuid4(), student_id=student_id, voice_test_id=test_id, unlocks_at=(taken_at or now) + UNLOCK_AFTER,
        ).on_conflict_do_nothing(index_elements=["student_id", "voice_test_id"]))

    rows = (await session.execute(select(McqTest).where(McqTest.student_id == student_id)
                                  .order_by(McqTest.unlocks_at.desc()))).scalars().all()
    done = set((await session.execute(
        select(Homework.test_id).join(StudentHomeworkProgress, StudentHomeworkProgress.homework_id == Homework.id)
        .where(StudentHomeworkProgress.student_id == student_id,
               StudentHomeworkProgress.status == StudentHomeworkStatus.COMPLETED))).scalars().all())
    due = [mcq.id for mcq in rows
           if mcq.unlocked_at is None and (mcq.voice_test_id in done or now >= mcq.unlocks_at)]
    unlocked = (await session.execute(
        update(McqTest).where(McqTest.id.in_(due), McqTest.unlocked_at.is_(None))
        .values(unlocked_at=now).returning(McqTest.id))).scalars().all() if due else []
    await session.flush()
    if unlocked:
        await session.commit()
        for mcq_id in unlocked:
            await push.notify(session, [student_id], "Your pick-one round is ready",
                              "See how much you've improved since your voice test.",
                              {"type": "mcq_test", "mcqTestId": str(mcq_id)})
    return list(rows)


# ------------------------------------------------------------ voice scores
@dataclass
class VoiceScores:
    mastery: int
    levels: list[BloomLevel]
    bloom: dict[str, int]  # level value -> percent
    concepts: dict[str, int]  # concept -> percent
    misconceptions: dict[str, list[str]] = field(default_factory=dict)


async def voice_scores(session: AsyncSession, student_id: uuid.UUID, test: VoiceTest) -> VoiceScores:
    """The 'before': the student's voice-test score per Bloom level and per
    concept (a concept scores the average of the test questions carrying it,
    each question scored from the grader's per-answer report)."""
    result = (await session.execute(select(StudentTestResult).where(
        StudentTestResult.test_id == test.id, StudentTestResult.student_id == student_id))).scalar_one_or_none()
    mastery = result.mastery_percent if result else 0
    bloom = {}
    if result:
        bloom = {level.value: pct for level, pct in (await session.execute(
            select(StudentTestResultBloomScore.bloom_level, StudentTestResultBloomScore.percent)
            .where(StudentTestResultBloomScore.student_test_result_id == result.id))).all() if pct is not None}

    questions = (await session.execute(select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test.id)
                                       .order_by(VoiceTestQuestion.order))).scalars().all()
    report = (await session.execute(
        select(AiAssessorSessionRecord.report).join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.student_id == student_id, AiAssessorSession.test_id == test.id,
               AiAssessorSessionRecord.report.is_not(None))
        .order_by(AiAssessorSessionRecord.created_at.desc()).limit(1))).scalar_one_or_none() or {}
    by_number = {e.get("question_number"): max(0, min(100, round(e["score_percent"])))
                 for e in report.get("qa_transcripts") or []
                 if isinstance(e, dict) and isinstance(e.get("score_percent"), (int, float))}

    levels = sorted({q.bloom_level for q in questions}, key=_BLOOM_ORDER.index)
    if not levels:
        levels = sorted((await session.execute(select(VoiceTestBloomLevel.bloom_level).where(
            VoiceTestBloomLevel.test_id == test.id))).scalars().all(), key=_BLOOM_ORDER.index) or [BloomLevel.UNDERSTAND]
    for level in levels:
        bloom.setdefault(level.value, mastery)

    per_concept: dict[str, list[int]] = {}
    misconceptions: dict[str, list[str]] = {}
    for q in questions:
        score = by_number.get(q.order, bloom.get(q.bloom_level.value, mastery))
        for concept in q.concepts or []:
            per_concept.setdefault(concept, []).append(score)
            misconceptions.setdefault(concept, []).extend(q.misconceptions or [])
    concepts = {c: round(sum(s) / len(s)) for c, s in per_concept.items()}
    if not concepts:  # a test prepared before concepts existed - the chapter is the one concept
        chapter = await session.get(Chapter, test.chapter_id)
        concepts = {chapter.name if chapter else "this chapter": mastery}
    return VoiceScores(mastery, levels, bloom, concepts, misconceptions)


# --------------------------------------------------------------- questions
def plan_slots(levels: list[BloomLevel], concepts: dict[str, int]) -> list[tuple[BloomLevel, str]]:
    """10 (level, concept) slots: levels round-robin then grouped (2 each for
    5 levels); concepts weakest first, cycling, with up to 2 strong concepts
    as a check on the last slots."""
    slot_levels = sorted((levels[i % len(levels)] for i in range(MCQ_QUESTION_COUNT)), key=_BLOOM_ORDER.index)
    ranked = sorted(concepts, key=lambda c: (concepts[c], c))
    weak = [c for c in ranked if concepts[c] < STRONG_PERCENT] or ranked
    strong = [c for c in reversed(ranked) if c not in weak][:MAX_STRONG_CHECKS]
    check_slots = {MCQ_QUESTION_COUNT - 1 - 2 * i: c for i, c in enumerate(strong)}
    # the last slot is the boss: Analyze at least
    slot_levels[-1] = max(slot_levels[-1], BloomLevel.ANALYZE, key=_BLOOM_ORDER.index)
    return [(level, check_slots.get(i) or weak[i % len(weak)]) for i, level in enumerate(slot_levels)]


def correct_index(options: list[str] | None, answer: str) -> int | None:
    """Index of the answer among exactly 4 distinct options (by text, or a
    bare letter like "b"/"(b)"), else None - such a question can't be used."""
    opts = [str(o).strip() for o in options or []]
    if len(opts) != 4 or len(set(opts)) != 4 or not all(opts):
        return None
    answer = (answer or "").strip()
    if answer in opts:
        return opts.index(answer)
    letter = answer.strip("()").lower()
    return "abcd".index(letter) if len(letter) == 1 and letter in "abcd" else None


@dataclass
class _Draft:
    bloom_level: BloomLevel
    concept: str
    text: str
    options: list[str]
    correct_index: int
    source_question_id: uuid.UUID | None = None
    explanation: str = ""
    hint: str = ""


_LABEL = re.compile(r"^\s*(?:\([a-dA-D]\)|[a-dA-D][.)])\s+")
# "option (A)", "Options B and D", "option (c)" - a reference to an option by its letter
# (a bare lowercase "option a" is left alone: it's usually the article)
_OPTION_LETTERS = re.compile(
    r"\b(?i:options?)\s*(?:\([A-Da-d]\)|[A-D])(?:\s*(?:,|and|or|&)\s*(?:\([A-Da-d]\)|[A-D]))*(?!\w)")
_LETTER = re.compile(r"(?<![A-Za-z])[A-Da-d](?![A-Za-z])")  # a lone letter, not one inside "options"/"and"


def unlabel(option: str) -> str:
    """An option without its own leading "(A)"/"A." label - the app letters the buttons."""
    return _LABEL.sub("", str(option)).strip() or str(option).strip()


def relabel(text: str, order: list[int]) -> str:
    """`text`'s "option (A)"-style letters moved with the options, after
    options = [options[i] for i in order]."""
    def letter(m: re.Match) -> str:
        abc = "ABCD" if m.group().isupper() else "abcd"
        return abc[order.index(abc.index(m.group()))]
    return _OPTION_LETTERS.sub(lambda m: _LETTER.sub(letter, m.group()), text or "")


def shuffle_options(options: list[str], index: int, explanation: str, seed: str) -> tuple[list[str], int, str]:
    """The options in a seeded order (models put the answer first), unlabelled, with
    the new answer index and the explanation's option letters following them."""
    order = list(range(len(options)))
    random.Random(seed).shuffle(order)
    return [unlabel(options[i]) for i in order], order.index(index), relabel(explanation, order)


def explanation_of(options: list[str], index: int, explanation: str | None = None) -> str:
    return (explanation or "").strip() or f"The answer is {options[index]}."


def hint_of(concept: str, answer: str, hint: str | None = None) -> str:
    """The "Ask Milo" nudge - falls back when it's empty or names the answer."""
    hint = (hint or "").strip()
    if not hint or re.search(rf"(?<!\w){re.escape(answer.strip())}(?!\w)", hint, re.IGNORECASE):
        return f"Think about {concept}."
    return hint


def _has_concept(q: DatasetQuestion, concept: str) -> bool:
    return concept.lower() in {c.lower() for c in q.concepts or []}


async def _concept_topics(session: AsyncSession, test: VoiceTest) -> tuple[dict[str, set], set]:
    """(concept -> the topics of the voice-test questions that carry it, every topic the
    test asked about): how a bank question (topic-tagged, rarely concept-tagged) is matched
    to a weak concept - the test's topics for a concept with none (an older test)."""
    by_concept: dict[str, set] = {}
    asked: set = set()
    for topic_id, concepts in (await session.execute(
            select(VoiceTestQuestion.topic_id, VoiceTestQuestion.concepts)
            .where(VoiceTestQuestion.voice_test_id == test.id, VoiceTestQuestion.topic_id.is_not(None)))).all():
        asked.add(topic_id)
        for c in concepts or []:
            by_concept.setdefault(c.lower(), set()).add(topic_id)
    return by_concept, asked


async def _bank(session: AsyncSession, student: User, test: VoiceTest) -> list[DatasetQuestion]:
    school_class = await session.get(SchoolClass, test.class_id)
    visible = await visible_datasets(session, student, subject_id=test.subject_id,
                                     grade=school_class.grade if school_class else None)
    dataset_ids = [d.id for d in visible if d.type == DatasetType.QA]
    if not dataset_ids:
        return []
    rows = (await session.execute(
        select(DatasetQuestion).where(
            DatasetQuestion.dataset_id.in_(dataset_ids), DatasetQuestion.chapter_id == test.chapter_id,
            DatasetQuestion.question_type.in_((QuestionType.MCQ, QuestionType.TRUE_FALSE)),
            DatasetQuestion.options.is_not(None), text_only_filter(pictures=True))
        .order_by(DatasetQuestion.id))).scalars().all()
    return [q for q in rows if correct_index(q.options, q.answer) is not None]


async def _generate(session, *, test, slots, open_slots, scores: VoiceScores, drafts, seed) -> None:
    if not open_slots or not generation_client.is_configured():
        return
    chapter_name, sections = await _sections(session, test)
    if not sections:
        return
    subject = await session.get(Subject, test.subject_id)
    school_class = await session.get(SchoolClass, test.class_id)
    payload = {
        "subject": subject.name if subject else "Science",
        "grade": school_class.grade if school_class else 10,
        "chapter": chapter_name,
        "sections": sections,
        "slots": [{"bloom_level": slots[i][0].value, "difficulty": "HARD" if i == len(slots) - 1 else "MEDIUM",
                   "question_type": "mcq",
                   "concept": slots[i][1],
                   # only this concept's: another question's misconceptions make off-topic distractors
                   "misconceptions": list(dict.fromkeys(scores.misconceptions.get(slots[i][1]) or []))[:6]}
                  for i in open_slots],
        "examples": [],
        "avoid": [d.text for d in drafts.values()],
    }
    try:
        result = await generation_client.generate_mcq(payload)
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("mcq_generation_unavailable", extra={"test_id": str(test.id), "open_slots": len(open_slots)})
        return
    for g in result.get("questions", []):
        index = correct_index(g.get("options"), g.get("answer", ""))
        if index is None or not 0 <= g.get("slot_index", -1) < len(open_slots):
            continue
        slot = open_slots[g["slot_index"]]
        options, index, explanation = shuffle_options([str(o).strip() for o in g["options"]], index,
                                                      g.get("explanation") or "", f"{seed}:{slot}")
        drafts[slot] = _Draft(slots[slot][0], slots[slot][1], g["text"], options, index,
                              explanation=explanation, hint=g.get("hint") or "")


async def ensure_questions(session: AsyncSession, mcq: McqTest, student: User) -> list[McqTestQuestion]:
    """The test's questions, built on first call. The McqTest row is locked
    while building so two concurrent opens can't both build."""
    await session.execute(select(McqTest.id).where(McqTest.id == mcq.id).with_for_update())
    existing = await questions_of(session, mcq.id)
    if existing:
        return existing
    test = await session.get(VoiceTest, mcq.voice_test_id)
    scores = await voice_scores(session, student.id, test)
    slots = plan_slots(scores.levels, scores.concepts)
    bank = await _bank(session, student, test)
    used: set[str] = set()

    def take(pred, pool=bank) -> DatasetQuestion | None:
        for q in pool:
            if question_uid(q) not in used and pred(q):
                used.add(question_uid(q))
                return q
        return None

    def from_bank(q: DatasetQuestion, concept: str) -> _Draft:
        # labelled with what the question is about (its gap_concept follows): a weak concept it is
        # tagged with - or, untagged, asked under its topic - else its own tag or its topic's name
        def on(c: str) -> bool:
            return _has_concept(q, c) if q.concepts else q.topic_id in (topics_of.get(c.lower()) or asked)
        concept = concept if on(concept) else next(
            (c for c in scores.concepts if on(c)), (q.concepts or [None])[0] or topic_names.get(q.topic_id) or concept)
        return _Draft(q.bloom_level, concept, q.text, [unlabel(o) for o in q.options],
                      correct_index(q.options, q.answer), q.id)

    drafts: dict[int, _Draft] = {}
    # The bank first: a checked question at the slot's level on the weak concept (tagged
    # with it, or on a topic the voice test asked it under) - cheap and already verified.
    # Only the slots it can't fill are designed by the generation service, aimed at the
    # student's own weak concepts with wrong options from their voice-test misconceptions.
    topics_of, asked = await _concept_topics(session, test)
    topic_names = {t.id: re.sub(r"^[\d.]+\s*", "", t.name) for t in (await session.execute(
        select(Topic).where(Topic.id.in_({q.topic_id for q in bank if q.topic_id})))).scalars()} if bank else {}
    for i, (level, concept) in enumerate(slots):
        on_concept = topics_of.get(concept.lower()) or asked
        q = take(lambda q: q.bloom_level == level and (_has_concept(q, concept) or q.topic_id in on_concept))
        if q is not None:
            drafts[i] = from_bank(q, concept)
    counts = {"bank": len(drafts)}
    # ponytail: holds this McqTest's row lock and a connection through the LLM call (~1 min);
    # prebuild() runs it right after homework is confirmed so the student rarely waits
    await _generate(session, test=test, slots=slots, open_slots=[i for i in range(len(slots)) if i not in drafts],
                    scores=scores, drafts=drafts, seed=mcq.id)
    counts["generated"] = len(drafts) - counts["bank"]
    boss = len(slots) - 1
    hardest_first = sorted(bank, key=lambda q: _BLOOM_ORDER.index(q.bloom_level), reverse=True)
    for i in sorted(range(len(slots)), key=lambda i: i != boss):  # the boss picks first
        level, concept = slots[i]
        if i not in drafts:
            q = take(lambda q: q.bloom_level == level) or take(lambda q: True, hardest_first if i == boss else bank)
            if q is not None:
                drafts[i] = from_bank(q, concept)
    counts["relaxed_bank"] = len(drafts) - counts["bank"] - counts["generated"]
    logger.info("mcq_questions_built", extra={"mcq_test_id": str(mcq.id), **counts})

    await _explain_bank(drafts)
    # anything still without them gets the deterministic fallbacks
    rows = [McqTestQuestion(mcq_test_id=mcq.id, order=order, bloom_level=d.bloom_level, concept=d.concept,
                            text=strip_inline_options(d.text, d.options), options=d.options,
                            correct_index=d.correct_index,
                            source_question_id=d.source_question_id,
                            explanation=explanation_of(d.options, d.correct_index, d.explanation),
                            hint=hint_of(d.concept, d.options[d.correct_index], d.hint),
                            is_boss=order == len(drafts),
                            gap_concept=d.concept if scores.concepts.get(d.concept, 100) < STRONG_PERCENT else None)
            for order, d in enumerate((drafts[i] for i in sorted(drafts)), start=1)]
    session.add_all(rows)
    await session.flush()
    return rows


async def _explain_bank(drafts: dict[int, "_Draft"]) -> None:
    """Bank questions come without a "why" or a hint: one batched generation
    call writes both. Best effort - on failure the fallbacks are used."""
    todo = {i: d for i, d in drafts.items() if not d.explanation or not d.hint}
    if not todo or not generation_client.is_configured():
        return
    items = [{"question_number": i + 1, "text": d.text, "options": d.options,
              "correct_index": d.correct_index, "concept": d.concept} for i, d in todo.items()]
    try:
        body = await generation_client.explain_mcq({"items": items})
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("mcq_explain_unavailable", extra={"count": len(items)})
        return
    for r in body.get("results") or []:
        d = todo.get(int(r.get("question_number", 0)) - 1)
        if d is not None:
            d.explanation = d.explanation or (r.get("explanation") or "")
            d.hint = d.hint or (r.get("hint") or "")


async def prebuild(mcq_test_id: uuid.UUID, student_id: uuid.UUID) -> None:
    """BackgroundTasks entrypoint after homework is confirmed: builds the MCQ
    test's questions in its own session so the student finds them ready."""
    async with get_session() as session:
        mcq = await session.get(McqTest, mcq_test_id)
        student = await session.get(User, student_id)
        if mcq is None or student is None:
            return
        try:
            await ensure_questions(session, mcq, student)
            await session.commit()
        except Exception:  # the open falls back to building on demand
            logger.exception("mcq_prebuild_failed", extra={"mcq_test_id": str(mcq_test_id)})
            await session.rollback()


async def questions_of(session: AsyncSession, mcq_test_id: uuid.UUID) -> list[McqTestQuestion]:
    return list((await session.execute(select(McqTestQuestion).where(McqTestQuestion.mcq_test_id == mcq_test_id)
                                       .order_by(McqTestQuestion.order))).scalars().all())


# -------------------------------------------------------------------- play
def _playable(mcq: McqTest) -> None:
    if status(mcq) == "locked":
        raise ConflictError("This MCQ test is not unlocked yet.")
    if status(mcq) == "completed":
        raise ConflictError("This MCQ test has already been attempted.")


async def answers_of(session: AsyncSession, mcq: McqTest) -> dict[uuid.UUID, McqQuestionAnswer]:
    """The student's first answers, by question id."""
    rows = (await session.execute(
        select(McqQuestionAnswer).join(McqTestQuestion, McqTestQuestion.id == McqQuestionAnswer.mcq_test_question_id)
        .where(McqTestQuestion.mcq_test_id == mcq.id, McqQuestionAnswer.student_id == mcq.student_id))).scalars().all()
    return {a.mcq_test_question_id: a for a in rows}


async def _record(session: AsyncSession, mcq: McqTest, q: McqTestQuestion, option_index: int,
                  elapsed_ms: int | None = None) -> None:
    """Keeps the first answer only (unique per question and student)."""
    await session.execute(pg_insert(McqQuestionAnswer).values(
        id=uuid.uuid4(), mcq_test_question_id=q.id, student_id=mcq.student_id, option_index=option_index,
        correct=option_index == q.correct_index, elapsed_ms=elapsed_ms,
    ).on_conflict_do_nothing(index_elements=["mcq_test_question_id", "student_id"]))


async def _question(session: AsyncSession, mcq: McqTest,
                    question_id: str) -> tuple[list[McqTestQuestion], McqTestQuestion]:
    questions = await questions_of(session, mcq.id)
    q = next((q for q in questions if str(q.id) == question_id), None)
    if q is None:
        raise NotFoundError("Question not found.")
    return questions, q


def xp_breakdown(questions: list[McqTestQuestion], answers: dict, *, speed_mode: bool,
                 lifelines: list[str]) -> tuple[dict, list[int]]:
    """XP from the first answers (objects with .correct and .elapsed_ms, by
    question id), and the run of consecutive correct answers ending at each
    question, in question order (an unanswered question breaks it)."""
    streaks, run = [], 0
    for q in questions:
        a = answers.get(q.id)
        run = run + 1 if a is not None and a.correct else 0
        streaks.append(run)
    right = [(q, answers[q.id]) for q in questions if q.id in answers and answers[q.id].correct]
    xp = {
        "correct": XP_PER_CORRECT * len(right),
        "combo_bonus": COMBO_BONUS * sum(s >= COMBO_FROM for s in streaks),
        "boss_bonus": BOSS_BONUS * sum(q.is_boss for q, _ in right),
        "speed_bonus": SPEED_BONUS * sum(a.elapsed_ms is not None and a.elapsed_ms <= SPEED_LIMIT_MS
                                         for _, a in right) if speed_mode else 0,
        "lifeline_cost": LIFELINE_COST * len(lifelines),
    }
    xp["total"] = max(0, xp["correct"] + xp["combo_bonus"] + xp["boss_bonus"] + xp["speed_bonus"]
                      - xp["lifeline_cost"])
    return xp, streaks


async def record_answer(session: AsyncSession, mcq: McqTest, question_id: str, option_index: int,
                        elapsed_ms: int | None) -> dict:
    """Marks one answer at once. Only the first answer counts: a repeat
    returns the stored result. Caller commits."""
    _playable(mcq)
    questions, q = await _question(session, mcq, question_id)
    await _record(session, mcq, q, option_index, elapsed_ms)
    answers = await answers_of(session, mcq)
    xp, streaks = xp_breakdown(questions, answers, speed_mode=mcq.speed_mode, lifelines=mcq.lifelines_used or [])
    return {"correct": answers[q.id].correct, "correct_index": q.correct_index,
            "explanation": explanation_of(q.options, q.correct_index, q.explanation),
            "combo": streaks[questions.index(q)], "xp_so_far": xp["total"]}


async def set_speed_mode(session: AsyncSession, mcq: McqTest, on: bool) -> None:
    """Only before the first answer. Caller commits."""
    _playable(mcq)
    if await answers_of(session, mcq):
        raise ConflictError("Speed mode can only be changed before the first answer.")
    mcq.speed_mode = on


async def use_lifeline(session: AsyncSession, mcq: McqTest, kind: str, question_id: str) -> dict:
    """50:50 hides two wrong options; Ask Milo gives the hint. Each once per
    test, on an unanswered question. Caller commits."""
    _playable(mcq)
    # lock the row so two taps can't both spend the same lifeline
    await session.execute(select(McqTest).where(McqTest.id == mcq.id).with_for_update()
                          .execution_options(populate_existing=True))
    used = list(mcq.lifelines_used or [])
    if kind in used:
        raise ConflictError("This lifeline has already been used.")
    _, q = await _question(session, mcq, question_id)
    if q.id in await answers_of(session, mcq):
        raise ConflictError("This question has already been answered.")
    mcq.lifelines_used = [*used, kind]
    if kind == "fifty_fifty":
        wrong = [i for i in range(len(q.options)) if i != q.correct_index]
        return {"hide_option_indexes": sorted(random.Random(f"{mcq.id}:{q.id}").sample(wrong, 2))}
    return {"hint": hint_of(q.concept, q.options[q.correct_index], q.hint)}


# ----------------------------------------------------------------- attempt
async def class_percentile(session: AsyncSession, mcq: McqTest, score: int) -> int | None:
    """% of classmates' completed checks of the same voice test this score beats."""
    others = (await session.execute(select(McqTest.score_percent).where(
        McqTest.voice_test_id == mcq.voice_test_id, McqTest.student_id != mcq.student_id,
        McqTest.completed_at.is_not(None), McqTest.score_percent.is_not(None)))).scalars().all()
    if len(others) < CLASS_PERCENTILE_MIN:
        return None
    return round(100 * sum(s < score for s in others) / len(others))


async def award_points(session: AsyncSession, student_id: uuid.UUID, xp: int) -> dict | None:
    """Adds the XP to the student's leaderboard points too (the leaderboard
    ranks by StudentPoints, not level XP) and returns their class rank before
    and after: 1 + classmates with more points, like the leaderboard's
    rank(). None when the student has no class."""
    row = await session.get(StudentPoints, student_id)
    if row is None:
        row = StudentPoints(student_id=student_id, points=0)
        session.add(row)
    before = row.points or 0
    row.points = before + xp
    class_id = (await session.execute(
        select(StudentProfile.class_id).where(StudentProfile.user_id == student_id))).scalar_one_or_none()
    if class_id is None:
        return None
    others = (await session.execute(
        select(func.coalesce(StudentPoints.points, 0)).select_from(StudentProfile)
        .outerjoin(StudentPoints, StudentPoints.student_id == StudentProfile.user_id)
        .where(StudentProfile.class_id == class_id, StudentProfile.user_id != student_id))).scalars().all()
    return {"before": 1 + sum(p > before for p in others), "after": 1 + sum(p > row.points for p in others)}


async def submit_attempt(session: AsyncSession, mcq: McqTest, answers: dict[str, int]) -> dict:
    """Marks the attempt from the first answers recorded while playing; an
    answer in `answers` (legacy clients send them all here) is recorded
    first for any question not answered yet. Stores it with its growth.
    409 when locked, not opened yet, or already attempted. Caller commits."""
    if status(mcq) == "locked":
        raise ConflictError("This MCQ test is not unlocked yet.")
    questions = await questions_of(session, mcq.id)
    if not questions:
        raise ConflictError("Open the MCQ test before submitting it.")
    now = datetime.now(timezone.utc)
    claimed = await session.execute(update(McqTest).where(McqTest.id == mcq.id, McqTest.completed_at.is_(None))
                                    .values(completed_at=now).returning(McqTest.id))
    if claimed.scalar_one_or_none() is None:
        raise ConflictError("This MCQ test has already been attempted.")
    for q in questions:
        if str(q.id) in answers:
            await _record(session, mcq, q, answers[str(q.id)])
    first = await answers_of(session, mcq)
    missed = [q for q in questions if not (q.id in first and first[q.id].correct)]

    by_level: dict[str, list[bool]] = {}
    by_concept: dict[str, list[bool]] = {}
    for q in questions:
        right = q not in missed
        by_level.setdefault(q.bloom_level.value, []).append(right)
        by_concept.setdefault(q.concept, []).append(right)
    correct = sum(sum(v) for v in by_level.values())
    score = percent(correct, len(questions))
    per_bloom = {k: percent(sum(v), len(v)) for k, v in by_level.items()}
    per_concept = {k: percent(sum(v), len(v)) for k, v in by_concept.items()}

    test = await session.get(VoiceTest, mcq.voice_test_id)
    before = await voice_scores(session, mcq.student_id, test)
    growth = growth_entries("bloom", per_bloom, before.bloom) + growth_entries("concept", per_concept, before.concepts)
    xp, streaks = xp_breakdown(questions, first, speed_mode=mcq.speed_mode, lifelines=mcq.lifelines_used or [])
    gaps = [q for q in questions if q.gap_concept]
    mcq.completed_at = now
    mcq.score_percent = score
    mcq.answers = [{"questionId": str(q.id), "optionIndex": first[q.id].option_index}
                   for q in questions if q.id in first]
    mcq.result = {"before": before.mastery, "after": corrected(score), "perBloom": per_bloom, "growth": growth}
    return {
        "score_percent": score,
        "per_bloom": per_bloom,
        "growth": growth,
        "mastery_before": before.mastery,
        "mastery_after": corrected(score),
        "correct_answers": [{"question_id": str(q.id), "option_index": q.correct_index} for q in questions],
        "xp_awarded": xp["total"],
        "xp_breakdown": xp,
        "best_combo": max(streaks, default=0),
        "gaps_fixed": {"fixed": sum(q not in missed for q in gaps), "total": len(gaps),
                       "concepts": list(dict.fromkeys(q.gap_concept for q in gaps if q not in missed))},
        "class_percentile": await class_percentile(session, mcq, score),
        "missed_question_ids": [str(q.id) for q in missed],
    }


