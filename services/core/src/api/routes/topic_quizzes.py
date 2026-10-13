"""Topic quizzes: quiz -> analysis -> homework -> re-check, per taught topic
(src/services/topic_quiz.py). Answers go through daily practice's
POST /me/daily-practice/{sessionId}/items/{itemId}/mcq - a quiz is a
practice session."""

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.routes.daily_practice import _get, _item_out, _teaches
from src.api.schemas.topic_quiz import (
    ChapterTopicQuizzesOut,
    QuizAnalysisOut,
    QuizFinishOut,
    QuizStartIn,
    TopicLoopOut,
    TopicLoopsOut,
    TopicQuizOut,
    TopicQuizSummaryOut,
)
from src.domain.models import Chapter, DailyPracticeSession, QuizTest, Role, Subject, Topic, User
from src.services import daily_practice, topic_quiz

router = APIRouter(prefix="/api/v1", tags=["topic-quizzes"])


def _analysis_out(quiz: DailyPracticeSession) -> QuizAnalysisOut | None:
    if quiz.outcome is None:
        return None
    a = quiz.analysis or {}
    return QuizAnalysisOut(
        score_percent=quiz.score_percent or 0, per_bloom=topic_quiz.ordered(quiz.per_bloom or {}),
        previous_per_bloom=topic_quiz.ordered(a.get("previous_per_bloom")), lagging=a.get("lagging") or [],
        summary=a.get("summary") or "", lagging_meaning=a.get("lagging_meaning") or "",
        need_to_know=a.get("need_to_know") or [], next_step=a.get("next_step") or "", outcome=quiz.outcome,
        homework_id=str(quiz.homework_id) if quiz.homework_id else None)


async def _quiz_out(session: AsyncSession, quiz: DailyPracticeSession) -> TopicQuizOut:
    """A topic quiz, or an attempt at a teacher's test (named after the test, no topic)."""
    items = await daily_practice.items_of(session, quiz.id)
    if quiz.quiz_test_id is not None:
        test = await session.get(QuizTest, quiz.quiz_test_id)
        topic_id, name = "", test.title
    else:
        topic = await session.get(Topic, quiz.topic_id)
        topic_id, name = str(topic.id), daily_practice.tidy(topic.name)
    return TopicQuizOut(id=str(quiz.id), topic_id=topic_id, topic_name=name, attempt=quiz.attempt,
                        status=quiz.status, items=[_item_out(*row) for row in items],
                        xp_awarded=quiz.xp_awarded or 0, analysis=_analysis_out(quiz),
                        quiz_test_id=str(quiz.quiz_test_id) if quiz.quiz_test_id else None)


@router.get("/me/topic-quizzes")
async def get_my_topic_loops(
    chapter_id: str | None = Query(default=None, alias="chapterId"),
    subject_id: str | None = Query(default=None, alias="subjectId"),
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicLoopsOut:
    """Every topic taught in the student's class (newest first) and where
    the student is on it - of one chapter or subject when given."""
    chapter = await _get(session, Chapter, chapter_id, "Chapter") if chapter_id else None
    subject = await _get(session, Subject, subject_id, "Subject") if subject_id else None
    loops = await topic_quiz.loop_of(session, user, chapter.id if chapter else None,
                                     subject.id if subject else None)
    return TopicLoopsOut(topics=[TopicLoopOut(
        topic_id=str(lp.topic.id), topic_name=daily_practice.tidy(lp.topic.name), taught_on=lp.taught_on,
        stage=lp.stage, quiz_id=str(lp.quiz.id) if lp.quiz else None,
        recheck_id=str(lp.recheck.id) if lp.recheck else None,
        homework_id=str(lp.quiz.homework_id) if lp.quiz and lp.quiz.homework_id else None,
        quiz_score=lp.quiz.score_percent if lp.quiz else None,
        recheck_score=lp.recheck.score_percent if lp.recheck else None) for lp in loops])


@router.post("/me/topic-quizzes/{topic_id}/start")
async def start_my_topic_quiz(
    topic_id: str,
    body: QuizStartIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicQuizOut:
    quiz = await topic_quiz.start(session, user, topic_id, body.attempt)
    await session.commit()
    return await _quiz_out(session, quiz)


@router.get("/me/topic-quizzes/quiz/{quiz_id}")
async def get_my_topic_quiz(
    quiz_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicQuizOut:
    return await _quiz_out(session, await topic_quiz.own_quiz(session, quiz_id, user))


@router.post("/me/topic-quizzes/quiz/{quiz_id}/finish")
async def finish_my_topic_quiz(
    quiz_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> QuizFinishOut:
    quiz = await topic_quiz.own_quiz(session, quiz_id, user)
    result = await topic_quiz.finish(session, quiz)
    await session.commit()
    if quiz.outcome == "homework" and quiz.homework_id is None:
        background_tasks.add_task(topic_quiz.build_homework, quiz.id)
    return QuizFinishOut(analysis=_analysis_out(quiz), xp_awarded=result["xp_awarded"],
                         streak_days=result["streak_days"])


@router.get("/classes/{class_id}/chapters/{chapter_id}/topic-quizzes")
async def get_class_topic_quizzes(
    class_id: str,
    chapter_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ChapterTopicQuizzesOut:
    chapter = await _get(session, Chapter, chapter_id, "Chapter")
    class_uuid = await _teaches(session, user, class_id, chapter.subject_id)
    rows = await topic_quiz.class_topic_summary(session, class_uuid, chapter.id)
    return ChapterTopicQuizzesOut(topics=[TopicQuizSummaryOut.model_validate(r) for r in rows])

