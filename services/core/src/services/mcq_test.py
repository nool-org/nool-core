"""The follow-up MCQ test after a voice test - closes the loop from "where
you lag" to "did it improve".

- Unlock: one McqTest per (student, voice test result), created lazily when
  the student lists their MCQ tests (sync_for_student). It unlocks once the
  student has confirmed that test's homework, or 48h after the voice test,
  whichever comes first.
- Questions (built on first open, ensure_questions): 10 = 2 per Bloom level
  of the voice test, on the student's weakest voice-test concepts plus up to
  2 strong ones as a check. Bank MCQs from the test's chapter first (4
  distinct options, answer among them); open slots generated through the
  generation service (distractors from the voice test's misconceptions);
  still-open slots take any unused bank MCQ of the chapter.
- Attempt (submit_attempt): marked here, one attempt only; growth math in
  services/growth.py.
"""

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    BloomLevel,
    Chapter,
    DatasetQuestion,
    DatasetType,
    Homework,
    McqTest,
    McqTestQuestion,
    QuestionType,
    SchoolClass,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    User,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestQuestion,
)
from src.repositories.dataset_access import visible_datasets
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
_BLOOM_ORDER = list(BloomLevel)


def status(mcq: McqTest) -> str:
    if mcq.completed_at is not None:
        return "completed"
    return "available" if mcq.unlocked_at is not None else "locked"


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
            await push.notify(session, [student_id], "Your MCQ check is ready",
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


def _has_concept(q: DatasetQuestion, concept: str) -> bool:
    return concept.lower() in {c.lower() for c in q.concepts or []}


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
            DatasetQuestion.options.is_not(None), text_only_filter())
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
    all_misconceptions = [m for ms in scores.misconceptions.values() for m in ms]
    payload = {
        "subject": subject.name if subject else "Science",
        "grade": school_class.grade if school_class else 10,
        "chapter": chapter_name,
        "sections": sections,
        "slots": [{"bloom_level": slots[i][0].value, "difficulty": "MEDIUM", "question_type": "mcq",
                   "concept": slots[i][1],
                   "misconceptions": list(dict.fromkeys(scores.misconceptions.get(slots[i][1]) or all_misconceptions))[:6]}
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
        options = [str(o).strip() for o in g["options"]]
        answer = options[index]
        random.Random(f"{seed}:{slot}").shuffle(options)  # models tend to put the answer first
        drafts[slot] = _Draft(slots[slot][0], slots[slot][1], g["text"], options, options.index(answer))


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

    def take(pred) -> DatasetQuestion | None:
        for q in bank:
            if question_uid(q) not in used and pred(q):
                used.add(question_uid(q))
                return q
        return None

    def from_bank(q: DatasetQuestion, concept: str) -> _Draft:
        concept = concept if _has_concept(q, concept) else next(
            (c for c in scores.concepts if _has_concept(q, c)), concept)
        return _Draft(q.bloom_level, concept, q.text, [str(o).strip() for o in q.options],
                      correct_index(q.options, q.answer), q.id)

    drafts: dict[int, _Draft] = {}
    for i, (level, concept) in enumerate(slots):
        q = take(lambda q: q.bloom_level == level and _has_concept(q, concept))
        if q is not None:
            drafts[i] = from_bank(q, concept)
    counts = {"bank": len(drafts)}
    # ponytail: holds this McqTest's row lock and a connection through the LLM call (~1 min);
    # release before generating like homework_builder if opens pile up
    await _generate(session, test=test, slots=slots, open_slots=[i for i in range(len(slots)) if i not in drafts],
                    scores=scores, drafts=drafts, seed=mcq.id)
    counts["generated"] = len(drafts) - counts["bank"]
    for i, (level, concept) in enumerate(slots):
        if i not in drafts:
            q = take(lambda q: q.bloom_level == level) or take(lambda q: True)
            if q is not None:
                drafts[i] = from_bank(q, concept)
    counts["relaxed_bank"] = len(drafts) - counts["bank"] - counts["generated"]
    logger.info("mcq_questions_built", extra={"mcq_test_id": str(mcq.id), **counts})

    rows = [McqTestQuestion(mcq_test_id=mcq.id, order=order, bloom_level=d.bloom_level, concept=d.concept,
                            text=d.text, options=d.options, correct_index=d.correct_index,
                            source_question_id=d.source_question_id)
            for order, d in enumerate((drafts[i] for i in sorted(drafts)), start=1)]
    session.add_all(rows)
    await session.flush()
    return rows


async def questions_of(session: AsyncSession, mcq_test_id: uuid.UUID) -> list[McqTestQuestion]:
    return list((await session.execute(select(McqTestQuestion).where(McqTestQuestion.mcq_test_id == mcq_test_id)
                                       .order_by(McqTestQuestion.order))).scalars().all())


# ----------------------------------------------------------------- attempt
async def submit_attempt(session: AsyncSession, mcq: McqTest, answers: dict[str, int]) -> tuple[dict, int]:
    """Marks the attempt and stores it with its growth. Returns (result,
    number correct). 409 when locked, not opened yet, or already attempted.
    Caller commits."""
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

    by_level: dict[str, list[bool]] = {}
    by_concept: dict[str, list[bool]] = {}
    for q in questions:
        right = answers.get(str(q.id)) == q.correct_index
        by_level.setdefault(q.bloom_level.value, []).append(right)
        by_concept.setdefault(q.concept, []).append(right)
    correct = sum(sum(v) for v in by_level.values())
    score = percent(correct, len(questions))
    per_bloom = {k: percent(sum(v), len(v)) for k, v in by_level.items()}
    per_concept = {k: percent(sum(v), len(v)) for k, v in by_concept.items()}

    test = await session.get(VoiceTest, mcq.voice_test_id)
    before = await voice_scores(session, mcq.student_id, test)
    growth = growth_entries("bloom", per_bloom, before.bloom) + growth_entries("concept", per_concept, before.concepts)
    mcq.completed_at = now
    mcq.score_percent = score
    mcq.answers = [{"questionId": k, "optionIndex": v} for k, v in answers.items()]
    mcq.result = {"before": before.mastery, "after": corrected(score), "perBloom": per_bloom, "growth": growth}
    return {
        "score_percent": score,
        "per_bloom": per_bloom,
        "growth": growth,
        "correct_answers": [{"question_id": str(q.id), "option_index": q.correct_index} for q in questions],
    }, correct
