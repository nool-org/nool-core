"""Daily practice: the student's 5-10 minutes on taught topics, and the
teacher's taught-topic toggle and class summary - see
src/services/daily_practice.py. Recorded answers: routes/practice_voice.py."""

import datetime as dt
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ForbiddenError, NotFoundError
from src.api.deps import get_db_session, require_role
from src.api.schemas.daily_practice import (
    ChapterProgressOut,
    ChapterRefOut,
    DailyPracticeCompleteOut,
    DailyPracticeSummaryOut,
    DailyPracticeTodayOut,
    McqResultOut,
    PracticeItemOut,
    PracticeMcqAnswerIn,
    PracticeMcqAnswerOut,
    PracticeResponseIn,
    PracticeResponseOut,
    SpecialResultOut,
    TopicProgressOut,
    TopicTaughtIn,
    VoiceFeedbackOut,
    VoiceResultOut,
)
from src.domain.models import Chapter, ClassTopicProgress, Role, StudentProfile, Topic, User
from src.repositories.lookups import get_class_in_school
from src.repositories.roster_repository import teaches_class, teaches_class_subject
from src.services import daily_practice, push, question_types, revision_cards

router = APIRouter(prefix="/api/v1", tags=["daily-practice"])
NOTIFY_QUIET_MINUTES = 30


# ------------------------------------------------------------------ student
def _special_result(item, question) -> SpecialResultOut:
    return SpecialResultOut(score=item.score or 0, correct=bool(item.correct),
                            message=(item.feedback or {}).get("message", ""), response=item.response or {},
                            reveal=question_types.reveal(item.kind, question.spec or {}))


def _item_out(item, question, topic_name) -> PracticeItemOut:
    result = None
    if item.answered_at is not None:
        result = (McqResultOut(correct=bool(item.correct), correct_index=question.correct_index,
                               explanation=daily_practice.explanation(question)) if item.kind == "mcq"
                  else _special_result(item, question) if item.kind in question_types.KINDS
                  else VoiceResultOut(transcript=item.transcript, score=item.score,
                                      feedback=VoiceFeedbackOut(**(item.feedback or {}))))
    return PracticeItemOut(
        id=str(item.id), order=item.order, kind=item.kind, topic_name=topic_name,
        bloom_level=item.bloom_level.value, review=item.review, text=question.text,
        # never the answer of an unanswered question: correct_index stays server-side
        options=question.options if item.kind == "mcq" else None,
        spec=question_types.public(item.kind, question.spec or {}) if item.kind in question_types.KINDS else None,
        answered=item.answered_at is not None, result=result)


@router.get("/me/daily-practice/today")
async def get_my_daily_practice(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> DailyPracticeTodayOut:
    practice, empty_reason = await daily_practice.ensure_today(session, user)
    await session.commit()
    chapter = await session.get(Chapter, practice.chapter_id) if practice and practice.chapter_id else None
    items = await daily_practice.items_of(session, practice.id) if practice else []
    new_items, new_topics = daily_practice.latest_extension(items)
    return DailyPracticeTodayOut(
        session_id=str(practice.id) if practice else None,
        date=practice.practice_date if practice else daily_practice.today_ist(),
        chapter=ChapterRefOut(id=str(chapter.id), name=chapter.name) if chapter else None,
        status=practice.status if practice else "none",
        items=[_item_out(*row) for row in items],
        xp_awarded=practice.xp_awarded if practice else 0,
        streak_days=await daily_practice.student_streak(session, user.id),
        empty_reason=empty_reason,
        new_items=new_items,
        new_topics=new_topics,
    )


@router.post("/me/daily-practice/{session_id}/items/{item_id}/mcq")
async def answer_my_daily_practice_mcq(
    session_id: str,
    item_id: str,
    body: PracticeMcqAnswerIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> PracticeMcqAnswerOut:
    _, item, question = await daily_practice.load_item_for_student(session, session_id, item_id, user)
    result = await daily_practice.answer_mcq(session, item, question, body.option_index, body.elapsed_ms)
    await session.commit()
    return PracticeMcqAnswerOut.model_validate(result)


@router.post("/me/daily-practice/{session_id}/items/{item_id}/response")
async def answer_my_practice_response(
    session_id: str,
    item_id: str,
    body: PracticeResponseIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> PracticeResponseOut:
    """Answers a who_is_right / make_your_own / fix_steps item - the first
    answer counts; a repeat returns the stored result."""
    _, item, question = await daily_practice.load_item_for_student(session, session_id, item_id, user)
    await daily_practice.answer_special(session, item, question, body.response)
    await session.commit()
    return PracticeResponseOut(result=_special_result(item, question), xp_awarded=item.xp_awarded or 0)


@router.post("/me/daily-practice/{session_id}/complete")
async def complete_my_daily_practice(
    session_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> DailyPracticeCompleteOut:
    practice = await daily_practice.own_session(session, session_id, user)
    result = await daily_practice.complete(session, practice)
    await session.commit()
    return DailyPracticeCompleteOut.model_validate(result)


# ------------------------------------------------------------------ teacher
async def _teaches(session: AsyncSession, user: User, class_id: str, subject_id=None) -> uuid.UUID:
    """The class's id, if it is in the teacher's school and taught by them
    (the chapter's subject, when one is given)."""
    school_class = await get_class_in_school(session, class_id, user.school_id)
    ok = (await teaches_class_subject(session, user.id, class_id, subject_id) if subject_id
          else await teaches_class(session, user.id, class_id))
    if not ok:
        raise ForbiddenError("That class isn't one of yours.")
    return school_class.id


async def _get(session: AsyncSession, model, id_: str, label: str):
    try:
        row = await session.get(model, uuid.UUID(id_))
    except ValueError:
        row = None
    if row is None:
        raise NotFoundError(f'{label} "{id_}" was not found.')
    return row


async def _chapter_topics(session: AsyncSession, chapter_id) -> list[Topic]:
    """In the same order as GET /chapters/{id}/topics."""
    return list((await session.execute(
        select(Topic).where(Topic.chapter_id == chapter_id).order_by(Topic.name))).scalars().all())


async def _taught_on(session: AsyncSession, class_id: uuid.UUID, topic_ids) -> dict:
    return dict((await session.execute(select(ClassTopicProgress.topic_id, ClassTopicProgress.taught_on).where(
        ClassTopicProgress.class_id == class_id, ClassTopicProgress.topic_id.in_(topic_ids)))).all())


@router.get("/classes/{class_id}/chapters/{chapter_id}/progress")
async def get_chapter_progress(
    class_id: str,
    chapter_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ChapterProgressOut:
    chapter = await _get(session, Chapter, chapter_id, "Chapter")
    class_uuid = await _teaches(session, user, class_id, chapter.subject_id)
    topics = await _chapter_topics(session, chapter.id)
    taught = await _taught_on(session, class_uuid, [t.id for t in topics])
    return ChapterProgressOut(
        chapter=ChapterRefOut(id=str(chapter.id), name=chapter.name),
        topics=[TopicProgressOut(id=str(t.id), name=t.name, order=n, taught_on=taught.get(t.id))
                for n, t in enumerate(topics, start=1)])


async def _notify_taught(session: AsyncSession, class_id: uuid.UUID, topic: Topic) -> None:
    """Tells the class's students the quiz on today's lesson is ready -
    once per lesson: skipped when another topic was marked in the last 30
    minutes (a teacher ticks several topics in a row)."""
    recent = (await session.execute(select(ClassTopicProgress.id).where(
        ClassTopicProgress.class_id == class_id, ClassTopicProgress.topic_id != topic.id,
        ClassTopicProgress.created_at > func.now() - dt.timedelta(minutes=NOTIFY_QUIET_MINUTES)).limit(1))).first()
    if recent:
        return
    student_ids = (await session.execute(
        select(StudentProfile.user_id).where(StudentProfile.class_id == class_id))).scalars().all()
    await push.notify(session, student_ids, "New topic quiz",
                      f"Your teacher just taught {daily_practice.tidy(topic.name)}. Take the quiz.",
                      {"type": "topic_quiz", "topicId": str(topic.id)})


@router.put("/classes/{class_id}/topics/{topic_id}/taught")
async def set_topic_taught(
    class_id: str,
    topic_id: str,
    body: TopicTaughtIn,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> TopicProgressOut:
    """taught=true marks (or re-dates) the topic as taught in the class -
    daily practice then asks about it (today's open sets pick it up, and a
    mark for today notifies the class); taught=false removes the mark."""
    topic = await _get(session, Topic, topic_id, "Topic")
    chapter = await session.get(Chapter, topic.chapter_id)
    class_uuid = await _teaches(session, user, class_id, chapter.subject_id)
    taught_on = None
    if body.taught:
        taught_on = body.taught_on or daily_practice.today_ist()
        await session.execute(pg_insert(ClassTopicProgress).values(
            id=uuid.uuid4(), class_id=class_uuid, chapter_id=chapter.id, topic_id=topic.id, taught_on=taught_on,
            marked_by=user.id,
        ).on_conflict_do_update(index_elements=["class_id", "topic_id"],
                                set_={"taught_on": taught_on, "marked_by": user.id}))
    else:
        await session.execute(delete(ClassTopicProgress).where(
            ClassTopicProgress.class_id == class_uuid, ClassTopicProgress.topic_id == topic.id))
    await session.commit()
    if body.taught:
        # revision cards are made once per topic; have them ready before students look
        background_tasks.add_task(revision_cards.pregenerate_for_topic, topic.id)
    if taught_on == daily_practice.today_ist():
        await _notify_taught(session, class_uuid, topic)
    order = [t.id for t in await _chapter_topics(session, chapter.id)].index(topic.id) + 1
    return TopicProgressOut(id=str(topic.id), name=topic.name, order=order, taught_on=taught_on)


@router.get("/classes/{class_id}/daily-practice/summary")
async def get_daily_practice_summary(
    class_id: str,
    date: dt.date | None = None,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> DailyPracticeSummaryOut:
    class_uuid = await _teaches(session, user, class_id)
    summary = await daily_practice.class_summary(session, class_uuid, date or daily_practice.today_ist())
    return DailyPracticeSummaryOut.model_validate(summary)
