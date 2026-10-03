from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError
from shared.logging import get_logger

from src.api.deps import get_db_session, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.homework import HomeworkQuestionOut
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
    User,
    VoiceTest,
)
from src.services.colearner_insight_client import ColearnerInsightUnavailableError, generate_homework_insight
from src.services.practice_bank import archive_homework_questions

router = APIRouter(prefix="/api/v1", tags=["homework-student"])
logger = get_logger(__name__)


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


async def _ensure_insight_generated(session: AsyncSession, hw: Homework, student_id) -> dict:
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
) -> HomeworkLearningContextOut:
    await _progress_in(session, homework_id, user.id)
    result = await session.execute(select(Homework).where(Homework.id == homework_id))
    hw = result.scalar_one_or_none()
    if hw is None:
        raise NotFoundError(f'No Homework with id "{homework_id}".')

    insight = await _ensure_insight_generated(session, hw, user.id)
    return HomeworkLearningContextOut(
        homework_id=str(hw.id),
        topic_label=hw.gap_topic,
        what_needs_understanding=insight["what_needs_understanding"],
        references=[HomeworkReference(**ref) for ref in insight["references"]],
        key_idea_title=insight["key_idea_title"],
        key_idea_body=insight["key_idea_body"],
        connection_prompt=insight["connection_prompt"],
    )


@router.get("/me/homework/{homework_id}/questions")
async def get_my_homework_questions(
    homework_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[HomeworkQuestionOut]:
    """The offline-capable Q&A set - the client caches this on-device."""
    await _progress_in(session, homework_id, user.id)
    result = await session.execute(
        select(HomeworkQuestion)
        .where(HomeworkQuestion.homework_id == homework_id)
        .order_by(HomeworkQuestion.order)
    )
    items = [
        HomeworkQuestionOut(
            id=str(q.id),
            homework_id=homework_id,
            order=q.order,
            bloom_level=q.bloom_level,
            dataset_id=str(q.dataset_id),
            text=q.text,
            answer=q.answer,
        )
        for q in result.scalars().all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/me/homework/{homework_id}/confirm-completion")
async def confirm_completion(
    homework_id: str,
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

    await session.commit()
    return HomeworkQaConfirmationOut(confirmed_at=progress.confirmed_at)
