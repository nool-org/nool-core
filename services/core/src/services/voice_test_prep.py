"""Picks a Voice Test's questions once - one dataset question per Bloom
level, the same for every student of the test - and has the generation
service reword each for speech and write its grading rubric. Runs as a
background task after POST /tests (see voice_test.py's create_test); the
teacher can then review/approve/swap them (voice_test.py's /questions).

Never fails silently: generation down -> the dataset text is used as-is
(no rubric); no question for a level -> that level is skipped; no question
at all -> the co-learner improvises from textbook_context, as before. Each
case logs a warning. VoiceTest.prep_status tells sessions whether to wait
(PENDING) or go (READY / FAILED). Questions that can't be asked aloud - MCQ
stems, {tex} markup, broken encoding - are never picked (logged).
"""

import logging
import re
import uuid

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
    if not picked:
        logger.warning("voice_test_prep_no_questions", extra={"test_id": str(test.id)})
        return False

    for number, (question, prepped) in enumerate(zip(picked, await _prep(test, ctx, picked)), start=1):
        session.add(VoiceTestQuestion(voice_test_id=test.id, order=number, **_fields(question, prepped)))
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


def _unspeakable(question: DatasetQuestion) -> str | None:
    """Why a dataset question can't be asked aloud, or None when it can."""
    text = question.text or ""
    if question.question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE) or _LISTED_OPTIONS.search(text):
        return "options"  # only makes sense with options to read
    if "{tex}" in text.lower():
        return "tex"
    if "\ufffd" in text:  # stray encoding characters
        return "encoding"
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
    gave nothing back - the caller then keeps the dataset text."""
    payload = {
        "grade": ctx["grade"], "subject": ctx["subject"], "chapter": ctx["chapter"].name, "board": ctx["board"],
        "levels": [q.bloom_level.value for q in questions],
        "candidates": [{
            "id": str(q.id), "text": q.text, "answer": q.answer, "bloom_level": q.bloom_level.value,
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
    prepped = [by_id.get(str(q.id)) for q in questions]
    if missing := [str(q.id) for q, p in zip(questions, prepped) if p is None]:
        logger.warning("voice_test_prep_question_not_reworded",
                       extra={"test_id": str(test.id), "dataset_question_ids": missing})
    return prepped


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
