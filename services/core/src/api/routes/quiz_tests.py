"""Teacher-made written tests (services/quiz_tests.py) - create (questions
are picked in the background), review, publish now or schedule, results;
students start them from their Tests tab and answer through daily
practice's item routes, then finish with the topic quiz's finish."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.routes.topic_quizzes import _quiz_out
from src.api.schemas.quiz_tests import (
    QuizTestCreateIn,
    QuizTestDetailOut,
    QuizTestListOut,
    QuizTestOut,
    QuizTestQuestionOut,
    QuizTestResultsOut,
    QuizTestScheduleIn,
)
from src.api.schemas.topic_quiz import TopicQuizOut
from src.domain.models import DailyPracticeSession, QuizTest, Role, StudentProfile, User
from src.services import quiz_tests

router = APIRouter(prefix="/api/v1", tags=["quiz-tests"])


def _out(test: QuizTest, done: int = 0, students: int = 0) -> QuizTestOut:
    return QuizTestOut(done_count=done, student_count=students,
        id=str(test.id), class_id=str(test.class_id), subject_id=str(test.subject_id),
        chapter_id=str(test.chapter_id), title=test.title, topic_ids=test.topic_ids, mix=test.mix,
        status=test.status, due_at=test.due_at,
        opens_at=test.published_at if test.status in ("published", "scheduled") else None,
        created_at=test.created_at, question_count=len(test.question_ids))


@router.post("/classes/{class_id}/quiz-tests", status_code=201)
async def create_quiz_test(
    class_id: str,
    body: QuizTestCreateIn,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuizTestOut:
    test = await quiz_tests.create(session, user, class_id, body.chapter_id, body.topic_ids, body.mix, body.title,
                                   body.due_at)
    await session.commit()
    background_tasks.add_task(quiz_tests.prepare, test.id)
    return _out(test)


@router.get("/quiz-tests")
async def list_my_quiz_tests(
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuizTestListOut:
    rows = (await session.execute(select(QuizTest).where(QuizTest.created_by == user.id)
                                  .order_by(QuizTest.created_at.desc()))).scalars().all()
    done = dict((await session.execute(
        select(DailyPracticeSession.quiz_test_id, func.count()).where(
            DailyPracticeSession.quiz_test_id.in_([t.id for t in rows]), DailyPracticeSession.attempt == 1,
            DailyPracticeSession.outcome.is_not(None)).group_by(DailyPracticeSession.quiz_test_id))).all()) if rows else {}
    students = dict((await session.execute(
        select(StudentProfile.class_id, func.count()).where(StudentProfile.class_id.in_({t.class_id for t in rows}))
        .group_by(StudentProfile.class_id))).all()) if rows else {}
    return QuizTestListOut(items=[_out(t, done.get(t.id, 0), students.get(t.class_id, 0)) for t in rows])


@router.get("/quiz-tests/{test_id}")
async def get_quiz_test(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuizTestDetailOut:
    """With every question and its answer, for the teacher's review."""
    test = await quiz_tests.own_test(session, user, test_id)
    questions = [QuizTestQuestionOut(
        id=str(q.id), kind=q.kind, bloom_level=q.bloom_level.value, topic_id=str(q.topic_id) if q.topic_id else None,
        text=q.text, options=q.options, correct_index=q.correct_index, spec=q.spec, explanation=q.explanation)
        for q in await quiz_tests.questions_of(session, test)]
    return QuizTestDetailOut(**_out(test).model_dump(), questions=questions)


@router.post("/quiz-tests/{test_id}/publish")
async def publish_quiz_test(
    test_id: str,
    body: QuizTestScheduleIn,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuizTestOut:
    """Opens it now, or schedules it (opensAt). Students are told when it opens now."""
    test = await quiz_tests.own_test(session, user, test_id)
    opened = await quiz_tests.publish(session, test, body.opens_at)
    await session.commit()
    if opened:
        await quiz_tests.notify_class(session, test)
    return _out(test)


@router.get("/quiz-tests/{test_id}/results")
async def get_quiz_test_results(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuizTestResultsOut:
    test = await quiz_tests.own_test(session, user, test_id)
    return QuizTestResultsOut.model_validate(await quiz_tests.results(session, test))


@router.post("/me/quiz-tests/{test_id}/start")
async def start_my_quiz_test(
    test_id: str,
    attempt: Annotated[int, Query(ge=1, le=2)] = 1,  # 2 = the second look, after the homework
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicQuizOut:
    attempt = await quiz_tests.start(session, user, test_id, attempt)
    await session.commit()
    return await _quiz_out(session, attempt)
