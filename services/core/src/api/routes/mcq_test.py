"""C5: the follow-up MCQ test (student) and Bloom growth (teacher) - see
src/services/mcq_test.py and src/services/growth.py."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError
from src.api.deps import get_db_session, require_role
from src.api.routes.gamification import apply_xp
from src.api.schemas.mcq_test import (
    LifelineIn,
    LifelineOut,
    McqAnsweredOut,
    McqAnswerIn,
    McqAnswerOut,
    McqAttemptIn,
    McqAttemptOut,
    McqQuestionOut,
    McqRetryIn,
    McqRetryOut,
    McqTestItemOut,
    McqTestListOut,
    McqTestOut,
    SpeedModeIn,
    SpeedModeOut,
    TestGrowthOut,
)
from src.domain.models import Chapter, McqTest, McqTestQuestion, Role, Topic, User, VoiceTest
from src.repositories.lookups import get_test_for_teacher
from src.services import growth, mcq_test

router = APIRouter(prefix="/api/v1", tags=["mcq-tests"])


async def _title(session: AsyncSession, voice_test_id) -> str:
    """'Chapter · Topic' so two checks on one chapter read differently."""
    chapter, topic = (await session.execute(
        select(Chapter.name, Topic.name).select_from(VoiceTest)
        .join(Chapter, Chapter.id == VoiceTest.chapter_id)
        .outerjoin(Topic, Topic.id == VoiceTest.topic_id)
        .where(VoiceTest.id == voice_test_id))).one_or_none() or (None, None)
    if topic and topic.isupper():
        topic = topic.title()  # the textbook graph stores headings in capitals
    return " · ".join(filter(None, [chapter or "Chapter", topic]))


async def _own(session: AsyncSession, mcq_test_id: str, user: User) -> McqTest:
    """The student's own MCQ test, with its unlock state refreshed."""
    for mcq in await mcq_test.sync_for_student(session, user.id):
        if str(mcq.id) == mcq_test_id:
            return mcq
    raise NotFoundError("MCQ test not found.")


@router.get("/me/mcq-tests")
async def list_my_mcq_tests(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> McqTestListOut:
    rows = await mcq_test.sync_for_student(session, user.id)
    await session.commit()
    counts = dict((await session.execute(
        select(McqTestQuestion.mcq_test_id, func.count()).where(McqTestQuestion.mcq_test_id.in_([m.id for m in rows]))
        .group_by(McqTestQuestion.mcq_test_id))).all()) if rows else {}
    items = []
    for mcq in rows:
        state = mcq_test.status(mcq)
        items.append(McqTestItemOut(
            id=str(mcq.id), test_id=str(mcq.voice_test_id), title=await _title(session, mcq.voice_test_id),
            status=state, unlocks_at=mcq.unlocks_at if state == "locked" else None,
            # not built until first opened - the planned count until then
            question_count=counts.get(mcq.id, mcq_test.MCQ_QUESTION_COUNT),
        ))
    # same chapter and topic tested twice: tell them apart by the test date
    dates = dict((await session.execute(
        select(VoiceTest.id, VoiceTest.created_at).where(VoiceTest.id.in_([m.voice_test_id for m in rows])))).all())         if rows else {}
    seen = [i.title for i in items]
    for item, mcq in zip(items, rows):
        if seen.count(item.title) > 1 and mcq.voice_test_id in dates:
            item.title = f"{item.title} · {dates[mcq.voice_test_id]:%-d %b}"
    return McqTestListOut(items=items)


@router.get("/me/mcq-tests/{mcq_test_id}")
async def get_my_mcq_test(
    mcq_test_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> McqTestOut:
    mcq = await _own(session, mcq_test_id, user)
    if mcq_test.status(mcq) == "locked":
        await session.commit()
        raise ConflictError("This MCQ test is not unlocked yet.")
    questions = await mcq_test.ensure_questions(session, mcq, user)
    await session.commit()
    answers = await mcq_test.answers_of(session, mcq)
    return McqTestOut(
        id=str(mcq.id), test_id=str(mcq.voice_test_id), title=await _title(session, mcq.voice_test_id),
        # never the answer of an unanswered question: correct_index stays server-side
        questions=[McqQuestionOut(id=str(q.id), text=q.text, options=q.options, bloom_level=q.bloom_level.value,
                                  concept=q.concept, is_boss=q.is_boss, gap_concept=q.gap_concept)
                   for q in questions],
        speed_mode=mcq.speed_mode,
        lifelines_left=[k for k in mcq_test.LIFELINES if k not in (mcq.lifelines_used or [])],
        answered=[McqAnsweredOut(question_id=str(q.id), option_index=answers[q.id].option_index,
                                 correct=answers[q.id].correct, correct_index=q.correct_index,
                                 explanation=mcq_test.explanation_of(q.options, q.correct_index, q.explanation))
                  for q in questions if q.id in answers],
    )


@router.post("/me/mcq-tests/{mcq_test_id}/speed-mode")
async def set_my_mcq_speed_mode(
    mcq_test_id: str,
    body: SpeedModeIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> SpeedModeOut:
    mcq = await _own(session, mcq_test_id, user)
    await mcq_test.set_speed_mode(session, mcq, body.on)
    await session.commit()
    return SpeedModeOut(speed_mode=mcq.speed_mode)


@router.post("/me/mcq-tests/{mcq_test_id}/answers")
async def answer_my_mcq_question(
    mcq_test_id: str,
    body: McqAnswerIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> McqAnswerOut:
    mcq = await _own(session, mcq_test_id, user)
    result = await mcq_test.record_answer(session, mcq, body.question_id, body.option_index, body.elapsed_ms)
    await session.commit()
    return McqAnswerOut.model_validate(result)


@router.post("/me/mcq-tests/{mcq_test_id}/lifelines")
async def use_my_mcq_lifeline(
    mcq_test_id: str,
    body: LifelineIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> LifelineOut:
    mcq = await _own(session, mcq_test_id, user)
    result = await mcq_test.use_lifeline(session, mcq, body.kind, body.question_id)
    await session.commit()
    return LifelineOut.model_validate(result)


@router.post("/me/mcq-tests/{mcq_test_id}/attempt")
async def submit_my_mcq_attempt(
    mcq_test_id: str,
    body: McqAttemptIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> McqAttemptOut:
    mcq = await _own(session, mcq_test_id, user)
    result = await mcq_test.submit_attempt(session, mcq, {a.question_id: a.option_index for a in body.answers})
    rank = await mcq_test.award_points(session, user.id, result["xp_awarded"])
    await apply_xp(session, user.id, result["xp_awarded"])
    await session.commit()
    return McqAttemptOut.model_validate({**result, "rank": rank})


@router.post("/me/mcq-tests/{mcq_test_id}/retry")
async def retry_my_mcq_misses(
    mcq_test_id: str,
    body: McqRetryIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> McqRetryOut:
    mcq = await _own(session, mcq_test_id, user)
    result = await mcq_test.submit_retry(session, mcq, {a.question_id: a.option_index for a in body.answers})
    await mcq_test.award_points(session, user.id, result["xp_awarded"])
    await apply_xp(session, user.id, result["xp_awarded"])
    await session.commit()
    return McqRetryOut.model_validate(result)


@router.get("/tests/{test_id}/growth")
async def get_test_growth(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> TestGrowthOut:
    test = await get_test_for_teacher(session, test_id, user)
    return TestGrowthOut.model_validate(await growth.class_growth(session, test))
