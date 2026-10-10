"""C5: the follow-up MCQ test (student) and Bloom growth (teacher) - see
src/services/mcq_test.py and src/services/growth.py."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError
from src.api.deps import get_db_session, require_role
from src.api.routes.gamification import apply_xp
from src.api.schemas.mcq_test import (
    McqAttemptIn,
    McqAttemptOut,
    McqQuestionOut,
    McqTestItemOut,
    McqTestListOut,
    McqTestOut,
    TestGrowthOut,
)
from src.domain.models import Chapter, McqTest, McqTestQuestion, Role, User, VoiceTest
from src.repositories.lookups import get_test_for_teacher
from src.services import growth, mcq_test

router = APIRouter(prefix="/api/v1", tags=["mcq-tests"])


async def _title(session: AsyncSession, voice_test_id) -> str:
    name = (await session.execute(select(Chapter.name).join(VoiceTest, VoiceTest.chapter_id == Chapter.id)
                                  .where(VoiceTest.id == voice_test_id))).scalar_one_or_none()
    return f"{name or 'Chapter'} - MCQ check"


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
    return McqTestOut(
        id=str(mcq.id), test_id=str(mcq.voice_test_id), title=await _title(session, mcq.voice_test_id),
        # never the answer: correct_index stays server-side
        questions=[McqQuestionOut(id=str(q.id), text=q.text, options=q.options, bloom_level=q.bloom_level.value,
                                  concept=q.concept) for q in questions],
    )


@router.post("/me/mcq-tests/{mcq_test_id}/attempt")
async def submit_my_mcq_attempt(
    mcq_test_id: str,
    body: McqAttemptIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> McqAttemptOut:
    mcq = await _own(session, mcq_test_id, user)
    result, correct = await mcq_test.submit_attempt(
        session, mcq, {a.question_id: a.option_index for a in body.answers})
    xp = correct * mcq_test.XP_PER_CORRECT
    await apply_xp(session, user.id, xp)
    await session.commit()
    return McqAttemptOut.model_validate({**result, "xp_awarded": xp})


@router.get("/tests/{test_id}/growth")
async def get_test_growth(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> TestGrowthOut:
    test = await get_test_for_teacher(session, test_id, user)
    return TestGrowthOut.model_validate(await growth.class_growth(session, test))
