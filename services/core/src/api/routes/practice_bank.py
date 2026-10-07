from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.practice_bank import PracticeBankEntryOut
from src.domain.models import BloomLevel, PracticeBankEntry, Role, Subject, User

router = APIRouter(prefix="/api/v1", tags=["practice-bank"])


@router.get("/me/practice-bank")
async def list_my_practice_bank(
    subject_id: str | None = Query(default=None, alias="subjectId"),
    chapter_id: str | None = Query(default=None, alias="chapterId"),
    bloom_level: BloomLevel | None = Query(default=None, alias="bloomLevel"),
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[PracticeBankEntryOut]:
    """Every question this student has ever archived out of a completed
    Homework, across every Homework - the read side of
    src/services/practice_bank.py's archive_homework_questions. Filters
    are ANDed and all optional; the student picks their own subset for a
    practice session client-side from this full list (see nool-apps'
    practice-bank screen) rather than the server ever deciding a subset.
    """
    stmt = select(PracticeBankEntry).where(PracticeBankEntry.student_id == user.id)
    if subject_id is not None:
        stmt = stmt.where(PracticeBankEntry.subject_id == subject_id)
    if chapter_id is not None:
        stmt = stmt.where(PracticeBankEntry.chapter_id == chapter_id)
    if bloom_level is not None:
        stmt = stmt.where(PracticeBankEntry.bloom_level == bloom_level)
    stmt = stmt.order_by(PracticeBankEntry.archived_at.desc())

    result = await session.execute(stmt)
    entries = result.scalars().all()
    subject_ids = {e.subject_id for e in entries if e.subject_id}
    subjects = {s.id: s for s in (await session.execute(
        select(Subject).where(Subject.id.in_(subject_ids)))).scalars().all()} if subject_ids else {}
    items = [
        PracticeBankEntryOut(
            id=str(e.id),
            source_homework_id=str(e.source_homework_id),
            subject_id=str(e.subject_id) if e.subject_id else None,
            subject_name=subjects[e.subject_id].name if e.subject_id in subjects else None,
            subject_grade=subjects[e.subject_id].grade if e.subject_id in subjects else None,
            chapter_id=str(e.chapter_id) if e.chapter_id else None,
            topic_id=str(e.topic_id) if e.topic_id else None,
            bloom_level=e.bloom_level,
            text=e.text,
            answer=e.answer,
            archived_at=e.archived_at,
        )
        for e in entries
    ]
    return ListEnvelope(items=items, total=len(items))
