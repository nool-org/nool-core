import asyncio
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import undefer
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError
from shared.logging import get_logger

from src.api.deps import get_db_session, require_role
from src.api.schemas.common import ListEnvelope
from src.api.routes.gamification import apply_xp
from src.api.schemas.homework import HomeworkAnswerIn, HomeworkAnswerOut, StudentHomeworkQuestionOut
from src.api.schemas.student_homework import (
    CurrentHomeworkResponse,
    HomeworkLearningContextOut,
    HomeworkQaConfirmationOut,
    HomeworkReference,
    StudentHomeworkOverviewOut,
)
from src.domain.models import (
    Chapter,
    DailyPracticeSession,
    Homework,
    HomeworkQuestion,
    HomeworkStatus,
    Role,
    SchoolClass,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    Topic,
    User,
    VoiceTest,
)
from src.domain.models.homework import HomeworkAnswer
from src.services.question_selector import figure_svgs
from src.services.colearner_insight_client import ColearnerInsightUnavailableError, generate_homework_insight
from src.services.practice_bank import archive_homework_questions
from src.services import daily_practice, homework_autogen, mcq_test, student_tasks

router = APIRouter(prefix="/api/v1", tags=["homework-student"])
logger = get_logger(__name__)

# Homework XP is for finishing it, once - answering questions earns none
COMPLETE_XP = 30


async def _progress_in(session: AsyncSession, homework_id: str, student_id) -> StudentHomeworkProgress:
    result = await session.execute(
        select(StudentHomeworkProgress).where(
            StudentHomeworkProgress.homework_id == homework_id,
            StudentHomeworkProgress.student_id == student_id,
        )
    )
    progress = result.scalar_one_or_none()
    if progress is None:
        raise NotFoundError("This Homework isn't assigned to you.")
    return progress


@router.get("/me/homework/current")
async def get_current_homework(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> CurrentHomeworkResponse:
    """homework: null when nothing is assigned right now - a real empty state.
    With several open, the newest one that hasn't expired."""
    result = await session.execute(
        select(StudentHomeworkProgress, Homework)
        .join(Homework, Homework.id == StudentHomeworkProgress.homework_id)
        .where(
            StudentHomeworkProgress.student_id == user.id,
            StudentHomeworkProgress.status != StudentHomeworkStatus.COMPLETED,
        )
        .order_by(StudentHomeworkProgress.expired, Homework.created_at.desc(), Homework.id)
        .limit(1)
    )
    row = result.first()
    if row is None:
        return CurrentHomeworkResponse(homework=None)
    return _overview(*row)


@router.get("/me/homework/{homework_id}")
async def get_my_homework(
    homework_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> CurrentHomeworkResponse:
    """One of the caller's homework by id (same envelope as /current) - 404
    unless it's assigned to them."""
    progress = await _progress_in(session, homework_id, user.id)
    return _overview(progress, await session.get(Homework, progress.homework_id))


def _overview(progress: StudentHomeworkProgress, hw: Homework) -> CurrentHomeworkResponse:
    return CurrentHomeworkResponse(
        homework=StudentHomeworkOverviewOut(
            homework_id=str(hw.id),
            gap_topic=hw.gap_topic,
            total_questions=hw.total_questions,
            qa_completed=progress.qa_completed,
            expired=progress.expired,
        )
    )


async def _textbook_reference(session: AsyncSession, topic_id) -> dict | None:
    """"Read it in your textbook": the NCERT chapter and the page the topic
    starts on (Topic.page) - a fact, so it isn't left to the generated insight."""
    topic = await session.get(Topic, topic_id) if topic_id else None
    if topic is None or topic.page is None:
        return None
    chapter = await session.get(Chapter, topic.chapter_id)
    subject = await session.get(Subject, chapter.subject_id) if chapter else None
    title = f"NCERT {subject.name if subject else 'textbook'}"
    if chapter is not None:
        title += f" · Chapter {chapter.order_index}"
    return {"title": title, "subtitle": f"{daily_practice.tidy(topic.name)} · page {topic.page}"}


def _placeholder_insight(gap_topic: str) -> dict:
    """Same generic shape this endpoint always returned before real
    insight generation existed - used whenever generation hasn't
    succeeded yet, so the screen never has nothing to show.
    """
    return {
        "what_needs_understanding": f"You need more practice with {gap_topic}.",
        "references": [{"title": "Class notes", "subtitle": gap_topic}],
        "key_idea_title": gap_topic,
        "key_idea_body": f"A quick refresher on {gap_topic}.",
        "connection_prompt": f"How does {gap_topic} connect to what you already know?",
    }


# homework id -> the background run writing its insight (warm_insight), so an
# open that lands mid-run waits for that one instead of starting a second
_insight_runs: dict = {}


async def warm_insight(homework_id, student_id, token: str | None) -> None:
    """Writes a just-built homework's insight in the background (own session),
    so opening it doesn't wait on the model. Needs the student's token
    (colearner's auth); without one, the first open writes it as before.
    Logged, never raised."""
    from src.repositories import get_session

    if not token or homework_id in _insight_runs:
        return

    async def run() -> None:
        async with get_session() as session:
            hw = await session.get(Homework, homework_id)
            if hw is not None:
                await _ensure_insight_generated(session, hw, student_id, token)

    task = asyncio.get_running_loop().create_task(run())
    _insight_runs[homework_id] = task
    try:
        await task
    except Exception:
        logger.exception("homework_insight_warm_failed", extra={"homework_id": str(homework_id)})
    finally:
        _insight_runs.pop(homework_id, None)


async def _ensure_insight_generated(session: AsyncSession, hw: Homework, student_id, token: str | None = None) -> dict:
    """Generates this Homework's real, LLM-grounded insight exactly once
    and persists it - see Homework.insight_generated_at's doc comment on
    the model. Every call after the first is a plain read of the cached
    columns; only the very first GET for a given Homework pays the LLM
    latency/cost. Never raises - a generation failure (colearner
    unreachable, no API key, malformed output) falls back to the same
    generic placeholder this endpoint always returned, and simply isn't
    cached, so the next GET tries again.
    """
    running = _insight_runs.get(hw.id)
    if hw.insight_generated_at is None and running is not None and running is not asyncio.current_task():
        await asyncio.wait({running})  # the background run is writing it right now
        await session.refresh(hw)
    if hw.insight_generated_at is not None:
        return {
            "what_needs_understanding": hw.insight_what_needs_understanding,
            "references": hw.insight_references,
            "key_idea_title": hw.insight_key_idea_title,
            "key_idea_body": hw.insight_key_idea_body,
            "connection_prompt": hw.insight_connection_prompt,
        }

    test = await session.get(VoiceTest, hw.test_id) if hw.test_id else None
    topic = await session.get(Topic, hw.topic_id) if hw.topic_id else None
    chapter_id = test.chapter_id if test else topic.chapter_id if topic else hw.chapter_id
    chapter = await session.get(Chapter, chapter_id) if chapter_id else None
    subject = await session.get(Subject, chapter.subject_id) if chapter else None
    school_class = await session.get(SchoolClass, hw.class_id)

    weak_bloom_level = "UNDERSTAND"
    result_row = await session.execute(
        select(StudentTestResult).where(
            StudentTestResult.test_id == hw.test_id, StudentTestResult.student_id == student_id
        )
    )
    test_result = result_row.scalar_one_or_none() if test else None
    if hw.topic_id is not None:  # a topic quiz's homework: its weakest level
        lagging = (await session.execute(select(DailyPracticeSession.analysis).where(
            DailyPracticeSession.homework_id == hw.id))).scalar_one_or_none() or {}
        weak_bloom_level = (lagging.get("lagging") or [weak_bloom_level])[0]
    if test_result is not None:
        scores_row = await session.execute(
            select(StudentTestResultBloomScore).where(
                StudentTestResultBloomScore.student_test_result_id == test_result.id
            )
        )
        assessed = [s for s in scores_row.scalars().all() if s.percent is not None]
        if assessed:
            weak_bloom_level = min(assessed, key=lambda s: s.percent).bloom_level.value

    # the same gap the questions target: a challenge's homework is built on the student's
    # weakest topic (homework_autogen.weak_spots) - the explanation must be about that one too
    gap = topic
    if gap is None and test is not None:
        focus = (await homework_autogen.weak_spots(session, test_id=test.id, student_id=student_id)).get("focus")
        gap = await session.get(Topic, focus.topic_id) if focus and focus.topic_id else None
    gap_name = f"{hw.gap_topic} - {daily_practice.tidy(gap.name)}" if gap and topic is None else hw.gap_topic

    try:
        insight = await generate_homework_insight(
            topic=gap_name[:200],
            grade=f"Grade {school_class.grade}" if school_class else "Grade 10",
            subject=subject.name if subject else hw.gap_topic,
            mastery_percent=hw.gap_mastery_percent,
            weak_bloom_level=weak_bloom_level,
            textbook_context=(gap.text_content if gap else None) or (test.textbook_context if test else None)
            or (chapter.name if chapter else None),
            token=token,
        )
    except ColearnerInsightUnavailableError as exc:
        logger.warning(
            "homework_insight_generation_failed",
            extra={"homework_id": str(hw.id), "reason": str(exc)},
        )
        return _placeholder_insight(hw.gap_topic)

    hw.insight_what_needs_understanding = insight["what_needs_understanding"]
    hw.insight_references = insight["references"]
    hw.insight_key_idea_title = insight["key_idea_title"]
    hw.insight_key_idea_body = insight["key_idea_body"]
    hw.insight_connection_prompt = insight["connection_prompt"]
    hw.insight_generated_at = datetime.now(timezone.utc)
    await session.commit()
    return insight


@router.get("/me/homework/{homework_id}/context")
async def get_homework_context(
    homework_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
    authorization: Annotated[str | None, Header()] = None,
) -> HomeworkLearningContextOut:
    await _progress_in(session, homework_id, user.id)
    result = await session.execute(select(Homework).where(Homework.id == homework_id))
    hw = result.scalar_one_or_none()
    if hw is None:
        raise NotFoundError(f'No Homework with id "{homework_id}".')

    token = authorization.split(" ", 1)[-1].strip() if authorization else None
    insight = await _ensure_insight_generated(session, hw, user.id, token)
    if hw.topic_id is not None:
        topic_id = hw.topic_id
        chapter_id = (await session.execute(select(Topic.chapter_id).where(Topic.id == topic_id))).scalar()
    else:
        chapter_id = hw.chapter_id or (await session.execute(
            select(VoiceTest.chapter_id).where(VoiceTest.id == hw.test_id))).scalar()
        topic_id = (await session.execute(select(Topic.id).where(
            Topic.chapter_id == chapter_id, Topic.name == hw.gap_topic).limit(1))).scalar() if chapter_id else None
    book = await _textbook_reference(session, topic_id)
    return HomeworkLearningContextOut(
        homework_id=str(hw.id),
        topic_label=hw.gap_topic,
        what_needs_understanding=insight["what_needs_understanding"],
        references=[HomeworkReference(**ref) for ref in ([book] if book else []) + insight["references"]],
        key_idea_title=insight["key_idea_title"],
        key_idea_body=insight["key_idea_body"],
        connection_prompt=insight["connection_prompt"],
        chapter_id=str(chapter_id) if chapter_id else None,
        topic_id=str(topic_id) if topic_id else None,
    )


@router.get("/me/homework/{homework_id}/questions")
async def get_my_homework_questions(
    homework_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[StudentHomeworkQuestionOut]:
    """The offline-capable Q&A set - the client caches this on-device. No
    answers: the student writes their own."""
    await _progress_in(session, homework_id, user.id)
    result = await session.execute(
        select(HomeworkQuestion)
        .options(undefer(HomeworkQuestion.is_boss))
        .where(HomeworkQuestion.homework_id == homework_id)
        .order_by(HomeworkQuestion.order)
    )
    rows = result.scalars().all()
    svgs = await figure_svgs(session, [q.source_question_id for q in rows])
    items = [
        StudentHomeworkQuestionOut(
            id=str(q.id),
            homework_id=homework_id,
            order=q.order,
            bloom_level=q.bloom_level,
            dataset_id=str(q.dataset_id),
            text=q.text,
            is_boss=q.is_boss,
            figure_svg=svgs.get(q.source_question_id),
        )
        for q in rows
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/me/homework/{homework_id}/confirm-completion")
async def confirm_completion(
    homework_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkQaConfirmationOut:
    """The only call that flips qaCompleted. Local recall progress never
    syncs on its own.

    Also the only place StudentHomeworkProgress.status and the parent
    Homework's status/completed_count ever move - before this fix they
    were dead fields, never written anywhere, so GET /me/homework/current's
    `!= COMPLETED` filter never actually excluded a finished Homework and
    the teacher's Homework list never reflected real progress. Idempotent
    on replay: re-confirming an already-COMPLETED progress row just
    refreshes `confirmed_at` and re-derives the same counts.

    Also archives this Homework's questions into the student's own
    Practice Bank (src/services/practice_bank.py) - same idempotency
    guarantee, safe to run again on replay.
    """
    progress = await _progress_in(session, homework_id, user.id)
    first_time = progress.status != StudentHomeworkStatus.COMPLETED
    progress.qa_completed = True
    # the first completion time stands - it decides the early-finish bonus
    progress.confirmed_at = progress.confirmed_at if not first_time and progress.confirmed_at else datetime.now(
        timezone.utc)
    progress.status = StudentHomeworkStatus.COMPLETED
    await session.flush()

    hw = await session.get(Homework, homework_id)
    early_xp = completion_xp = 0
    if hw is not None and first_time:
        early_xp = student_tasks.early_bonus_xp(hw.created_at, hw.completion_window_hours, progress.confirmed_at)
        completion_xp = COMPLETE_XP + early_xp
        await mcq_test.award_points(session, user.id, completion_xp)
        await apply_xp(session, user.id, completion_xp)
    if hw is not None:
        completed = await session.execute(
            select(func.count()).where(
                StudentHomeworkProgress.homework_id == homework_id,
                StudentHomeworkProgress.status == StudentHomeworkStatus.COMPLETED,
            )
        )
        hw.completed_count = completed.scalar_one()
        if hw.completed_count >= hw.assigned_count and hw.assigned_count > 0:
            hw.status = HomeworkStatus.COMPLETED
        elif hw.status == HomeworkStatus.ASSIGNED:
            hw.status = HomeworkStatus.IN_PROGRESS

        await archive_homework_questions(session, homework=hw, student_id=user.id)

    # Homework done unlocks this test's MCQ check now; build its questions in
    # the background so it's ready when the app opens it
    mcq_id = None
    if hw is not None and hw.test_id is not None:
        mcq = next((m for m in await mcq_test.sync_for_student(session, user.id)
                    if m.voice_test_id == hw.test_id), None)
        if mcq is not None and mcq.completed_at is None:
            mcq_id = mcq.id
            background_tasks.add_task(mcq_test.prebuild, mcq.id, user.id)

    # a teacher's challenge's homework: its second look opens now
    recheck_test = (await session.execute(select(DailyPracticeSession.quiz_test_id).where(
        DailyPracticeSession.homework_id == hw.id, DailyPracticeSession.quiz_test_id.is_not(None))
    )).scalars().first() if hw is not None and hw.topic_id else None

    await session.commit()
    return HomeworkQaConfirmationOut(confirmed_at=progress.confirmed_at,
                                     mcq_test_id=str(mcq_id) if mcq_id else None,
                                     recheck_topic_id=str(hw.topic_id) if hw is not None and hw.topic_id
                                     and recheck_test is None else None,
                                     recheck_test_id=str(recheck_test) if recheck_test else None,
                                     early_bonus_xp=early_xp, xp_awarded=completion_xp)


@router.post("/me/homework/{homework_id}/questions/{question_id}/answer")
async def answer_question(
    homework_id: str,
    question_id: uuid.UUID,
    body: HomeworkAnswerIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkAnswerOut:
    """Saves the student's self-rating (and, for a multiple-choice question,
    the option they picked) for one question. No XP here - homework XP comes
    once, on finishing it (confirm_completion); xpAwarded stays 0."""
    progress = await _progress_in(session, homework_id, user.id)
    question = (await session.execute(
        select(HomeworkQuestion).options(undefer(HomeworkQuestion.is_boss))
        .where(HomeworkQuestion.id == question_id, HomeworkQuestion.homework_id == progress.homework_id)
    )).scalar_one_or_none()
    if question is None:
        raise NotFoundError("This question isn't part of this Homework.")

    values = {"self_rating": body.self_rating}
    if body.answer_text is not None:
        values["answer_text"] = body.answer_text
    await session.execute(
        insert(HomeworkAnswer)
        .values(id=uuid.uuid4(), homework_question_id=question.id, student_id=user.id, xp_awarded=0, **values)
        .on_conflict_do_update(index_elements=["homework_question_id", "student_id"], set_=values)
    )
    if progress.status == StudentHomeworkStatus.ASSIGNED:
        progress.status = StudentHomeworkStatus.IN_PROGRESS
    await session.commit()
    return HomeworkAnswerOut(saved=True, xp_awarded=0)
