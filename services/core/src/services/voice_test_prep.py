"""Picks a Voice Test's questions once - one dataset question per Bloom
level, the same for every student of the test - and has the generation
service reword each for speech and write its grading rubric. Runs as a
background task after POST /tests (see voice_test.py's create_test); the
teacher can then review/approve/swap them (voice_test.py's /questions).

Never fails silently: a question generation can't reword for speech is
swapped for another of its level, then dropped (never asked in raw dataset
wording); a level with no question gets one written to be spoken, else is
skipped; no question at all -> the co-learner improvises from textbook_context, as before. Each
case logs a warning. VoiceTest.prep_status tells sessions whether to wait
(PENDING) or go (READY / FAILED). Questions that can't be asked aloud - MCQ
stems, {tex} markup, broken encoding - are never picked (logged).
"""

import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    BloomLevel,
    Chapter,
    DatasetQuestion,
    DatasetType,
    QuestionType,
    School,
    SchoolClass,
    Subject,
    Topic,
    User,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestQuestion,
    VoiceTestQuestionStatus,
)
from src.domain.models.question_paper import PaperDifficultyLevel
from src.domain.models.voice_test import VoiceTestPrepStatus
from src.repositories import get_session
from src.repositories.dataset_access import visible_datasets
from src.services import generation_client, kg_client
from src.services.question_selector import Slot, select_questions

logger = logging.getLogger(__name__)

# select_questions only takes a question one difficulty step off the slot's
# within the test's topic - trying all three widens the pool to the chapter.
_DIFFICULTIES = (PaperDifficultyLevel.MEDIUM, PaperDifficultyLevel.EASY, PaperDifficultyLevel.HARD)


async def prepare_in_background(test_id: uuid.UUID, teacher_id: uuid.UUID) -> None:
    """BackgroundTasks entrypoint - its own session, create_test's is closed by now."""
    async with get_session() as session:
        test = await session.get(VoiceTest, test_id)
        teacher = await session.get(User, teacher_id)
        if test is None or teacher is None:
            return
        try:
            await prepare_test(session, test, teacher)
        except Exception:
            logger.exception("voice_test_prep_failed", extra={"test_id": str(test_id)})
            await session.rollback()
            test = await session.get(VoiceTest, test_id)
            test.prep_status = VoiceTestPrepStatus.FAILED  # never leave sessions blocked
            await session.commit()


async def prepare_test(session: AsyncSession, test: VoiceTest, teacher: User) -> None:
    """Sets test.prep_status: PENDING while it runs (sessions wait), then
    READY with questions or FAILED without (the co-learner improvises)."""
    test.prep_status = VoiceTestPrepStatus.PENDING
    await session.commit()
    test.prep_status = (VoiceTestPrepStatus.READY if await _prepare(session, test, teacher)
                        else VoiceTestPrepStatus.FAILED)
    await session.commit()


async def _prepare(session: AsyncSession, test: VoiceTest, teacher: User) -> bool:
    """True when the test has its questions."""
    ctx = await _context(session, test)
    if ctx is None:
        return False
    await _fill_textbook_context(test, ctx)
    await session.commit()

    if (await session.execute(
        select(VoiceTestQuestion.id).where(VoiceTestQuestion.voice_test_id == test.id).limit(1)
    )).first():
        return True  # already prepared (a retry) - the class must keep the same questions

    order = list(BloomLevel)
    levels = sorted((await session.execute(
        select(VoiceTestBloomLevel.bloom_level).where(VoiceTestBloomLevel.test_id == test.id)
    )).scalars().all(), key=order.index)
    dataset_ids = await _dataset_ids(session, teacher, test, ctx["grade"])
    unusable = await _unusable_uids(session, test, dataset_ids)

    picked: list[DatasetQuestion] = []
    for level in levels:
        question = await _pick(session, test, dataset_ids, level, {_uid(q) for q in picked} | unusable)
        if question is None:
            logger.warning("voice_test_prep_no_question_for_level",
                           extra={"test_id": str(test.id), "bloom_level": level.value})
            continue
        picked.append(question)

    prepped = await _prep(test, ctx, picked) if picked else []
    # A question generation could not reword for speech is swapped for another
    # of the same level (up to REWORD_RETRIES rounds), then dropped - never asked
    # in its raw dataset wording.
    tried = {_uid(q) for q in picked}
    for _ in range(REWORD_RETRIES):
        redo = [i for i, (q, p) in enumerate(zip(picked, prepped)) if not _reworded(q, p)]
        swaps = {}
        for i in redo:
            alt = await _pick(session, test, dataset_ids, picked[i].bloom_level, tried | unusable)
            if alt is not None:
                tried.add(_uid(alt))
                swaps[i] = alt
        if not swaps:
            break
        logger.info("voice_test_prep_retry_unreworded", extra={"test_id": str(test.id), "retried": len(swaps)})
        for i, p in zip(swaps, await _prep(test, ctx, list(swaps.values()))):
            if _reworded(swaps[i], p):
                picked[i], prepped[i] = swaps[i], p

    kept = [(q, p) for q, p in zip(picked, prepped) if _reworded(q, p)]
    if len(kept) < len(picked):
        logger.warning("voice_test_prep_dropped_unreworded", extra={
            "test_id": str(test.id), "dataset_question_ids": [_key(q) for q, p in zip(picked, prepped)
                                                              if not _reworded(q, p)]})
    if missing := [level for level in levels if level not in {q.bloom_level for q, _ in kept}]:
        written = await _write_for_levels(session, test, missing, examples=[q for q, _ in kept])
        if written:  # already written to be spoken - _prep only adds the rubric
            kept += zip(written, await _prep(test, ctx, written))
    if not kept:
        logger.warning("voice_test_prep_no_questions", extra={"test_id": str(test.id)})
        return False

    kept.sort(key=lambda qp: order.index(qp[0].bloom_level))
    for number, (question, p) in enumerate(kept, start=1):
        session.add(VoiceTestQuestion(voice_test_id=test.id, order=number, **_fields(question, p)))
    await session.commit()
    return True


async def swap_question(
    session: AsyncSession, test: VoiceTest, question: VoiceTestQuestion, teacher: User
) -> bool:
    """Replaces `question` in place (same id/order) by another dataset
    question of its Bloom level not already in the test, re-prepped.
    False when there is no other candidate."""
    ctx = await _context(session, test)
    if ctx is None:
        return False
    used = {
        external_uid or str(question_id) for external_uid, question_id in (await session.execute(
            select(DatasetQuestion.external_uid, DatasetQuestion.id)
            .join(VoiceTestQuestion, VoiceTestQuestion.dataset_question_id == DatasetQuestion.id)
            .where(VoiceTestQuestion.voice_test_id == test.id)
        )).all()
    }
    dataset_ids = await _dataset_ids(session, teacher, test, ctx["grade"])
    unusable = await _unusable_uids(session, test, dataset_ids)
    replacement = await _pick(session, test, dataset_ids, question.bloom_level, used | unusable)
    if replacement is None:
        return False
    [prepped] = await _prep(test, ctx, [replacement])
    for key, value in _fields(replacement, prepped).items():
        setattr(question, key, value)
    return True


def _key(question) -> str:
    """Candidate id sent to generation: the dataset row id, or a stable key for a written question."""
    return str(question.id) if question.id else f"written-{question.bloom_level.value}"


def _uid(question: DatasetQuestion) -> str:
    return question.external_uid or str(question.id)


async def _context(session: AsyncSession, test: VoiceTest) -> dict | None:
    chapter = await session.get(Chapter, test.chapter_id)
    subject = await session.get(Subject, test.subject_id)
    school_class = await session.get(SchoolClass, test.class_id)
    school = await session.get(School, school_class.school_id) if school_class else None
    if chapter is None or subject is None or school_class is None or school is None:
        return None
    topic = await session.get(Topic, test.topic_id) if test.topic_id else None
    return {"chapter": chapter, "subject": subject.name, "grade": school_class.grade, "board": school.board,
            "topic_names": [topic.name] if topic else []}


async def _fill_textbook_context(test: VoiceTest, ctx: dict) -> None:
    """Grounding text for the co-learner (kg). Best effort: without it the
    session still runs, just less grounded."""
    chapter = ctx["chapter"]
    if test.textbook_context or chapter.order_index is None:
        return  # done already / chapter never synced from KG - nothing to ground against
    levels = [level.value for level in BloomLevel]
    try:
        result = await kg_client.generate_assessor_content(
            subject=ctx["subject"], board=ctx["board"], grade=ctx["grade"],
            chapters=[{"number": chapter.order_index, "name": chapter.name}],
            topic_names=ctx["topic_names"], bloom_levels=levels, num_questions=2,
        )
    except Exception:
        logger.warning("voice_test_textbook_context_failed", extra={"test_id": str(test.id)})
        return
    test.textbook_context = result.get("textbook_context") or None


async def _dataset_ids(session: AsyncSession, teacher: User, test: VoiceTest, grade: int) -> list[uuid.UUID]:
    visible = await visible_datasets(session, teacher, subject_id=test.subject_id, grade=grade)
    return [d.id for d in visible if d.type == DatasetType.QA]


# A stem with its options written in: "... (a) 2 (b) 4 ..." / "... A. 2 B. 4 ...".
_LISTED_OPTIONS = re.compile(r"(?:^|\s)\(?[aA][.)]\s+\S.*(?:^|\s)\(?[bB][.)]\s+\S", re.S)


_BLANKS = re.compile(r"_{3,}|\bfill in\b", re.I)
_DRAWING = re.compile(r"\b(draw|diagram|sketch|label|figure|graph|plot)\b", re.I)
_PASSAGE = re.compile(r"read the (following|passage|text)|questions? that follows?|\btable\b|case[- ]based", re.I)
_PARTS = re.compile(r"(?:^|\s)\(?(?:i{1,3}|iv|[a-d])[.)]\s")  # (i) ... (ii) ... / a. ... b. ...
_OR_JOINED = re.compile(r"\s(?:OR|Or)\s")  # "State X. OR How is Y ..."
# Written work rewording can't turn into one spoken answer. ("write any four ..." is NOT here:
# generation narrows it to "tell me two or three ...".)
_WRITTEN_WORK = re.compile(r"\btabulate\b|match the following|\bcomplete the (?:table|equation)"
                           r"|\bbalance the (?:following )?(?:chemical )?equations?", re.I)
_CALCULATE = re.compile(r"\bcalculate\b|\bfind the value", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
MAX_SPOKEN_WORDS = 45  # longer than this is too much to hold in mind when heard once


def _unspeakable(question: DatasetQuestion) -> str | None:
    """Why a dataset question can't be asked aloud, or None when it can."""
    text = question.text or ""
    if question.question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE) or _LISTED_OPTIONS.search(text):
        return "options"  # only makes sense with options to read
    if "{tex}" in text.lower():
        return "tex"
    if "\ufffd" in text:  # stray encoding characters
        return "encoding"
    if _BLANKS.search(text):
        return "blanks"
    if _DRAWING.search(text):
        return "drawing"
    if _PASSAGE.search(text):
        return "passage"
    if _WRITTEN_WORK.search(text):
        return "written_work"
    if _CALCULATE.search(text) and len(_NUMBER.findall(text)) >= 3:
        return "long_numerical"  # a multi-step sum is worked on paper, not held in mind
    if len(_PARTS.findall(text)) >= 2 or _OR_JOINED.search(text):
        return "multipart"
    if len(text.split()) > MAX_SPOKEN_WORDS:
        return "too_long"
    return None


async def _unusable_uids(session: AsyncSession, test: VoiceTest, dataset_ids: list) -> set[str]:
    """The chapter's dataset questions a spoken test must never ask - logged."""
    if not dataset_ids:
        return set()
    questions = (await session.execute(select(DatasetQuestion).where(
        DatasetQuestion.dataset_id.in_(dataset_ids), DatasetQuestion.chapter_id == test.chapter_id,
    ))).scalars().all()
    skipped = {_uid(q): reason for q in questions if (reason := _unspeakable(q))}
    if skipped:
        logger.info("voice_test_prep_skipped_candidates", extra={
            "test_id": str(test.id), "skipped": len(skipped),
            "by_reason": {r: sum(1 for v in skipped.values() if v == r) for r in set(skipped.values())},
            "uids": sorted(skipped)[:50]})
    return set(skipped)


async def _pick(session, test, dataset_ids, level, exclude: set[str]) -> DatasetQuestion | None:
    for difficulty in _DIFFICULTIES:
        selection = await select_questions(
            session, dataset_ids=dataset_ids, chapter_id=test.chapter_id, topic_id=test.topic_id,
            slots=[Slot(level, difficulty)], exclude_uids=exclude, text_only=True,
        )
        if selection.by_slot and selection.by_slot[0] is not None:
            return selection.by_slot[0]
    return None


async def _prep(test: VoiceTest, ctx: dict, questions: list[DatasetQuestion]) -> list[dict | None]:
    """generation's rewording per question (same order), None where it
    gave nothing back - the caller then drops that question."""
    payload = {
        "grade": ctx["grade"], "subject": ctx["subject"], "chapter": ctx["chapter"].name, "board": ctx["board"],
        "levels": [q.bloom_level.value for q in questions],
        "candidates": [{
            "id": _key(q), "text": q.text, "answer": q.answer, "bloom_level": q.bloom_level.value,
            "topic_id": str(q.topic_id) if q.topic_id else None,
            "subtopic_id": str(q.subtopic_id) if q.subtopic_id else None,
            "concepts": list(q.concepts or []), "difficulty": q.difficulty.value if q.difficulty else None,
        } for q in questions],
    }
    try:
        out = await generation_client.prep_voice_test(payload)
        by_id = {str(p.get("dataset_question_id")): p for p in out.get("questions") or [] if isinstance(p, dict)}
    except Exception as exc:  # unavailable, or an unusable response
        logger.warning("voice_test_prep_generation_unavailable",
                       extra={"test_id": str(test.id), "reason": str(exc)})
        return [None] * len(questions)
    prepped = [by_id.get(_key(q)) for q in questions]
    if missing := [_key(q) for q, p in zip(questions, prepped) if p is None]:
        logger.warning("voice_test_prep_question_not_reworded",
                       extra={"test_id": str(test.id), "dataset_question_ids": missing})
    return prepped


@dataclass
class WrittenQuestion:
    """A question the generation service wrote for a Bloom level the bank had
    no speakable question for. Same attributes _prep/_fields read off a
    DatasetQuestion; it has no dataset row (id None)."""
    text: str
    answer: str
    bloom_level: BloomLevel
    topic_id: uuid.UUID | None = None
    subtopic_id: uuid.UUID | None = None
    concepts: list = field(default_factory=list)
    difficulty: None = None
    question_type: None = None
    id: None = None
    external_uid: None = None


async def _write_for_levels(session: AsyncSession, test: VoiceTest, levels: list[BloomLevel],
                            examples: list) -> list[WrittenQuestion]:
    """Levels with no speakable bank question get one written by the
    generation service, grounded in the chapter's textbook sections (the same
    grounding homework gap-fill uses). Never fakes one: none written -> the
    level is skipped as before."""
    from src.services.homework_builder import _sections  # shared textbook grounding

    if not generation_client.is_configured():
        return []
    chapter_name, sections = await _sections(session, test, test.topic_id)
    if not sections:
        return []
    subject = await session.get(Subject, test.subject_id)
    school_class = await session.get(SchoolClass, test.class_id)
    payload = {
        "subject": subject.name if subject else "Science",
        "grade": school_class.grade if school_class else 10,
        "chapter": chapter_name,
        "sections": sections,
        "slots": [{"bloom_level": level.value, "difficulty": "MEDIUM"} for level in levels],
        "examples": [{"text": q.text, "answer": (q.answer or "")[:400], "bloom_level": q.bloom_level.value,
                      "difficulty": None} for q in examples[:4]],
        "avoid": [q.text for q in examples],
        "style": "spoken",  # short, single question - it is asked aloud
    }
    try:
        result = await generation_client.gap_fill(payload)
    except Exception as exc:  # unavailable or unusable - the level is skipped, as before
        logger.warning("voice_test_prep_write_failed", extra={"test_id": str(test.id), "reason": str(exc)})
        return []
    written = []
    for q in result.get("questions") or []:
        if q.get("options") or not isinstance(q.get("slot_index"), int) or q["slot_index"] >= len(levels):
            continue
        candidate = WrittenQuestion(text=q["text"], answer=q.get("answer") or "",
                                    bloom_level=levels[q["slot_index"]], topic_id=test.topic_id)
        if _unspeakable(candidate) is None:
            written.append(candidate)
    logger.info("voice_test_prep_wrote_questions", extra={
        "test_id": str(test.id), "levels": [lvl.value for lvl in levels], "written": len(written)})
    return written


REWORD_RETRIES = 2


def _reworded(question: DatasetQuestion, prepped: dict | None) -> bool:
    if question.id is None:  # a WrittenQuestion is already written to be spoken - only its rubric is needed
        return prepped is not None
    text = ((prepped or {}).get("reworded_text") or "").strip()
    return bool(text) and text != (question.text or "").strip()


def _strings(value) -> list[str]:
    return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _fields(question: DatasetQuestion, prepped: dict | None) -> dict:
    prepped = prepped or {}
    reworded = (prepped.get("reworded_text") or "").strip() or question.text
    return {
        "dataset_question_id": question.id,
        "bloom_level": question.bloom_level,
        "topic_id": question.topic_id,
        "subtopic_id": question.subtopic_id,
        "concepts": _strings(prepped.get("concepts")) or list(question.concepts or []),
        "original_text": question.text,
        "reworded_text": reworded,
        "spoken_text": (prepped.get("spoken_text") or "").strip() or reworded,
        "expected_points": _strings(prepped.get("expected_points")),
        "misconceptions": _strings(prepped.get("misconceptions")),
        "hints": _strings(prepped.get("hints")),
        "status": VoiceTestQuestionStatus.DRAFT,
    }
