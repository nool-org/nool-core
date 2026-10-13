from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError
from src.api.deps import get_db_session, require_role
from src.services import subject_rules
from src.api.schemas.common import ListEnvelope
from src.api.schemas.admin_catalog import SubtopicOut
from src.api.schemas.curriculum import ChapterOut, SubjectOut, TopicOut
from src.domain.models import (
    Chapter,
    Role,
    SchoolCurriculum,
    Subject,
    Subtopic,
    TeacherClassAssignment,
    Topic,
    User,
)

router = APIRouter(prefix="/api/v1", tags=["curriculum"])


async def _ensure_subject_open_to(session: AsyncSession, user: User, subject_id) -> None:
    """The curriculum tree of a subject is readable by a school only once a
    Super Admin enabled that subject for it - and by a teacher only for a
    subject they teach there."""
    query = select(SchoolCurriculum.id).where(
        SchoolCurriculum.school_id == user.school_id, SchoolCurriculum.subject_id == subject_id,
        SchoolCurriculum.enabled.is_(True))
    if user.role == Role.TEACHER:
        query = query.where(select(TeacherClassAssignment.id).where(
            TeacherClassAssignment.teacher_id == user.id, TeacherClassAssignment.subject_id == subject_id).exists())
    if subject_id is None or (await session.execute(query.limit(1))).first() is None:
        raise NotFoundError("That subject isn't available to you.")


@router.get("/subjects")
async def list_subjects(
    grade: int | None = None,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SubjectOut]:
    """The subjects a Super Admin has enabled for the caller's school
    (school_curriculum). Only a Super Admin controls a school's subjects -
    a school with none enabled yet has none, no "everything" fallback."""
    result = await session.execute(
        select(Subject).join(SchoolCurriculum, SchoolCurriculum.subject_id == Subject.id).where(
            SchoolCurriculum.school_id == user.school_id, SchoolCurriculum.enabled.is_(True)
        )
    )
    # `grade` narrows to that grade's subjects (legacy grade-less subjects always match)
    items = [SubjectOut(id=str(s.id), name=s.name, grade=s.grade, voice_tests=subject_rules.voice_tests_allowed(s.name), formats=list(subject_rules.formats(s.name))) for s in result.scalars().all()
             if grade is None or s.grade in (None, grade)]
    return ListEnvelope(items=items, total=len(items))


@router.get("/classes/{class_id}/subjects")
async def list_subjects_for_class(
    class_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SubjectOut]:
    """The subjects this teacher is actually assigned to teach in this
    class (TeacherClassAssignment) - narrower than GET /subjects, which
    only scopes to the school. Scopes the Voice Test wizard's Subject
    step once a class has been chosen. An empty result is a legitimate
    outcome, not an error.

    Also narrowed to the school's currently-enabled subjects
    (school_curriculum), same discipline as GET /subjects above - a
    TeacherClassAssignment row is a separate table from school_curriculum,
    so nothing stops it from going stale (or being wrong from the start):
    a subject the school has since disabled, or was never really supposed
    to be assigned, must not keep surfacing here just because the
    assignment row still points at it.
    """
    assigned = await session.execute(
        select(Subject)
        .join(TeacherClassAssignment, TeacherClassAssignment.subject_id == Subject.id)
        .where(
            TeacherClassAssignment.teacher_id == user.id,
            TeacherClassAssignment.class_id == class_id,
        )
        .distinct()
    )
    assigned_subjects = list(assigned.scalars().all())
    if not assigned_subjects:
        return ListEnvelope(items=[], total=0)
    # only subjects the Super Admin has enabled for this school - an
    # assignment row can outlive a subject being switched off
    enabled = await session.execute(
        select(SchoolCurriculum.subject_id).where(
            SchoolCurriculum.school_id == user.school_id, SchoolCurriculum.enabled.is_(True)
        )
    )
    enabled_ids = {row[0] for row in enabled.all()}
    items = [SubjectOut(id=str(s.id), name=s.name, grade=s.grade, voice_tests=subject_rules.voice_tests_allowed(s.name), formats=list(subject_rules.formats(s.name))) for s in assigned_subjects if s.id in enabled_ids]
    return ListEnvelope(items=items, total=len(items))


@router.get("/subjects/{subject_id}/chapters")
async def list_chapters(
    subject_id: str,
    grade: int | None = None,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ChapterOut]:
    """`grade` is optional so existing callers keep working unfiltered -
    but every caller that already knows the class's grade (Voice Test/
    Question Paper setup, both scoped to a class before this step) should
    pass it. Subjects are per grade now (Subject.grade), so a subject's
    chapters are already one grade's; the filter still matters for legacy
    grade-less subjects. A chapter with no grade set (NULL) always matches,
    grade filter or not - it's "applies to any grade," not "belongs to no
    grade."
    """
    await _ensure_subject_open_to(session, user, subject_id)
    conditions = [Chapter.subject_id == subject_id]
    if grade is not None:
        conditions.append(or_(Chapter.grade == grade, Chapter.grade.is_(None)))
    result = await session.execute(
        select(Chapter).where(*conditions).order_by(Chapter.order_index.nulls_last(), Chapter.name))
    items = [
        ChapterOut(id=str(c.id), subject_id=subject_id, name=c.name, grade=c.grade)
        for c in result.scalars().all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/chapters/{chapter_id}/topics")
async def list_topics(
    chapter_id: str,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[TopicOut]:
    """Returns items: [] for a chapter with no authored topics yet - a
    real, expected empty state, not an error.
    """
    chapter = await session.get(Chapter, chapter_id)
    await _ensure_subject_open_to(session, user, chapter.subject_id if chapter else None)
    result = await session.execute(select(Topic).where(Topic.chapter_id == chapter_id).order_by(Topic.name))
    items = [TopicOut(id=str(t.id), chapter_id=chapter_id, name=t.name) for t in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/topics/{topic_id}/subtopics")
async def list_subtopics(
    topic_id: str,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SubtopicOut]:
    """Same as GET /chapters/{id}/topics one level down - e.g. for placing a
    question in a school's own question bank. Empty list when a topic has
    no subtopics."""
    subject_id = (await session.execute(
        select(Chapter.subject_id).join(Topic, Topic.chapter_id == Chapter.id).where(Topic.id == topic_id)
    )).scalar_one_or_none()
    await _ensure_subject_open_to(session, user, subject_id)
    result = await session.execute(select(Subtopic).where(Subtopic.topic_id == topic_id).order_by(Subtopic.name))
    items = [SubtopicOut(id=str(s.id), topic_id=topic_id, name=s.name) for s in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))
