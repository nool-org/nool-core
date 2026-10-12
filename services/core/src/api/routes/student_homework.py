import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from sqlalchemy import func, select, update
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
from src.services.colearner_insight_client import ColearnerInsightUnavailableError, generate_homework_insight
from src.services.practice_bank import archive_homework_questions
from src.services import mcq_test

router = APIRouter(prefix="/api/v1", tags=["homework-student"])
logger = get_logger(__name__)

ANSWER_XP = 10
BOSS_ANSWER_XP = 2 * ANSWER_XP
MIN_ANSWER_CHARS = 3  # shorter (or empty) answers earn no XP


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
    if hw.insight_generated_at is not None:
        return {
            "what_needs_understanding": hw.insight_what_needs_understanding,
            "references": hw.insight_references,
            "key_idea_title": hw.insight_key_idea_title,
            "key_idea_body": hw.insight_key_idea_body,
            "connection_prompt": hw.insight_connection_prompt,
        }

    test = await session.get(VoiceTest, hw.test_id)
    chapter = await session.get(Chapter, test.chapter_id) if test else None
    subject = await session.get(Subject, test.subject_id) if test else None
    school_class = await session.get(SchoolClass, test.class_id) if test else None

    weak_bloom_level = "UNDERSTAND"
    result_row = await session.execute(
        select(StudentTestResult).where(
            StudentTestResult.test_id == hw.test_id, StudentTestResult.student_id == student_id
        )
    )
    test_result = result_row.scalar_one_or_none()
    if test_result is not None:
        scores_row = await session.execute(
            select(StudentTestResultBloomScore).where(
                StudentTestResultBloomScore.student_test_result_id == test_result.id
            )
        )
        assessed = [s for s in scores_row.scalars().all() if s.percent is not None]
        if assessed:
            weak_bloom_level = min(assessed, key=lambda s: s.percent).bloom_level.value

    try:
        insight = await generate_homework_insight(
            topic=hw.gap_topic,
            grade=f"Grade {school_class.grade}" if school_class else "Grade 10",
            subject=subject.name if subject else hw.gap_topic,
            mastery_percent=hw.gap_mastery_percent,
            weak_bloom_level=weak_bloom_level,
            textbook_context=(test.textbook_context if test else None) or (chapter.name if chapter else None),
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
    chapter_id = (await session.execute(select(VoiceTest.chapter_id).where(VoiceTest.id == hw.test_id))).scalar()
    topic_id = (await session.execute(select(Topic.id).where(
        Topic.chapter_id == chapter_id, Topic.name == hw.gap_topic).limit(1))).scalar() if chapter_id else None
    return HomeworkLearningContextOut(
        homework_id=str(hw.id),
        topic_label=hw.gap_topic,
        what_needs_understanding=insight["what_needs_understanding"],
        references=[HomeworkReference(**ref) for ref in insight["references"]],
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
    items = [
        StudentHomeworkQuestionOut(
            id=str(q.id),
            homework_id=homework_id,
            order=q.order,
            bloom_level=q.bloom_level,
            dataset_id=str(q.dataset_id),
            text=q.text,
            is_boss=q.is_boss,
        )
        for q in result.scalars().all()
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
    progress.qa_completed = True
    progress.confirmed_at = datetime.now(timezone.utc)
    progress.status = StudentHomeworkStatus.COMPLETED
    await session.flush()

    hw = await session.get(Homework, homework_id)
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

    await session.commit()
    return HomeworkQaConfirmationOut(confirmed_at=progress.confirmed_at,
                                     mcq_test_id=str(mcq_id) if mcq_id else None)


@router.post("/me/homework/{homework_id}/questions/{question_id}/answer")
async def answer_question(
    homework_id: str,
    question_id: uuid.UUID,
    body: HomeworkAnswerIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> HomeworkAnswerOut:
    """Saves the student's answer and self-rating for one question. XP
    (double for the boss question) is awarded only the first time, and only
    if they actually wrote an answer - a repeat just updates the saved answer
    and returns xpAwarded 0 (so a blank first submit can't earn XP later)."""
    progress = await _progress_in(session, homework_id, user.id)
    question = (await session.execute(
        select(HomeworkQuestion).options(undefer(HomeworkQuestion.is_boss))
        .where(HomeworkQuestion.id == question_id, HomeworkQuestion.homework_id == progress.homework_id)
    )).scalar_one_or_none()
    if question is None:
        raise NotFoundError("This question isn't part of this Homework.")

    # XP only for a real written attempt, whichever rating
    attempted = len((body.answer_text or "").strip()) >= MIN_ANSWER_CHARS
    xp = (BOSS_ANSWER_XP if question.is_boss else ANSWER_XP) if attempted else 0
    # ON CONFLICT: two racing submits can't both count as the first
    created = (await session.execute(
        insert(HomeworkAnswer)
        .values(id=uuid.uuid4(), homework_question_id=question.id, student_id=user.id,
                answer_text=body.answer_text, self_rating=body.self_rating, xp_awarded=xp)
        .on_conflict_do_nothing(index_elements=["homework_question_id", "student_id"])
        .returning(HomeworkAnswer.id)
    )).scalar_one_or_none()
    if created is None:
        # a repeat: XP once if this is the first real written answer (a blank first tap earned 0);
        # the guarded UPDATE keeps two racing repeats from both earning it
        values = {"self_rating": body.self_rating}
        if body.answer_text is not None:
            values["answer_text"] = body.answer_text
        mine = (HomeworkAnswer.homework_question_id == question.id, HomeworkAnswer.student_id == user.id)
        earned = (await session.execute(
            update(HomeworkAnswer).where(*mine, HomeworkAnswer.xp_awarded == 0)
            .values(**values, xp_awarded=xp).returning(HomeworkAnswer.id)
        )).scalar_one_or_none() if xp else None
        if earned is None:
            xp = 0
            await session.execute(update(HomeworkAnswer).where(*mine).values(**values))
    if xp:
        await apply_xp(session, user.id, xp)
    if progress.status == StudentHomeworkStatus.ASSIGNED:
        progress.status = StudentHomeworkStatus.IN_PROGRESS
    await session.commit()
    return HomeworkAnswerOut(saved=True, xp_awarded=xp)
