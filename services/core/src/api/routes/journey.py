from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError

from src.api.deps import get_db_session, require_role
from src.api.schemas.journey import JourneyChapterNodeOut, SubjectJourneyOut
from src.domain.models import Chapter, Role, StudentChapterProgress, Subject, Topic, User

router = APIRouter(prefix="/api/v1", tags=["journey"])


@router.get("/me/journey")
async def get_my_journey(
    subject_id: str = Query(alias="subjectId"),
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> SubjectJourneyOut:
    """The "Journey" chapter map - a winding path through one subject's
    chapters. State is derived, not stored: walk `Chapter.order_index`
    order, the first chapter without a `StudentChapterProgress.completed_at`
    is CURRENT, everything before it is DONE, everything after is LOCKED.
    """
    subject_result = await session.execute(select(Subject).where(Subject.id == subject_id))
    subject = subject_result.scalar_one_or_none()
    if subject is None:
        raise NotFoundError(f'No Subject with id "{subject_id}".')

    chapters_result = await session.execute(
        select(Chapter)
        .where(Chapter.subject_id == subject_id)
        .order_by(Chapter.order_index.asc().nulls_last(), Chapter.name.asc())
    )
    chapters = list(chapters_result.scalars().all())

    progress_result = await session.execute(
        select(StudentChapterProgress).where(
            StudentChapterProgress.student_id == user.id,
            StudentChapterProgress.chapter_id.in_([c.id for c in chapters]),
        )
    )
    progress_by_chapter = {p.chapter_id: p for p in progress_result.scalars().all()}
    topic_counts = dict((await session.execute(
        select(Topic.chapter_id, func.count()).where(Topic.chapter_id.in_([c.id for c in chapters]))
        .group_by(Topic.chapter_id))).all())

    nodes: list[JourneyChapterNodeOut] = []
    still_unlocked = True
    for chapter in chapters:
        progress = progress_by_chapter.get(chapter.id)
        done = progress is not None and progress.completed_at is not None

        if done:
            state = "DONE"
        elif still_unlocked:
            state = "CURRENT"
        else:
            state = "LOCKED"

        nodes.append(
            JourneyChapterNodeOut(
                chapter_id=str(chapter.id),
                chapter_label=chapter.name,
                state=state,
                stars_earned=progress.stars if progress else 0,
                topic_count=topic_counts.get(chapter.id, 0),
            )
        )
        if not done:
            still_unlocked = False

    return SubjectJourneyOut(subject_id=str(subject.id), subject_label=subject.name, nodes=nodes)
