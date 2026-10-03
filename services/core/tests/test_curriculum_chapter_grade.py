"""GET /subjects/{id}/chapters?grade= must not merge two different
grades' chapters into one flat list - Subject is deliberately grade-
agnostic (one "Science" row spans 9th and 10th), so grade has to live on
Chapter and be filterable, or a 9th-grade book's ingested chapters would
show up identically in a 10th-grade class's chapter picker.
"""

import uuid

import pytest

from src.api.routes import curriculum
from src.domain.models import (
    Chapter, Role, School, SchoolClass, SchoolCurriculum, Subject, TeacherClassAssignment, User, UserStatus,
)


async def _seed_teacher_and_subject(db_session) -> tuple[User, Subject]:
    subject = Subject(name=f"Science-{uuid.uuid4()}")   # legacy grade-less: chapter.grade still filters
    school = School(name=f"S-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add_all([subject, school])
    await db_session.flush()
    teacher = User(
        firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    section = SchoolClass(school_id=school.id, grade=10, section="A")
    db_session.add_all([teacher, section])
    await db_session.flush()
    # the tree is readable once the subject is enabled for the school and the teacher teaches it
    db_session.add_all([SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True),
                        TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id)])
    await db_session.flush()
    return teacher, subject


@pytest.mark.asyncio
async def test_grade_filter_keeps_two_grades_chapters_from_bleeding_together(db_session):
    teacher, subject = await _seed_teacher_and_subject(db_session)
    ninth_chapter = Chapter(subject_id=subject.id, name="9th chapter", grade=9)
    tenth_chapter = Chapter(subject_id=subject.id, name="10th chapter", grade=10)
    db_session.add_all([ninth_chapter, tenth_chapter])
    await db_session.flush()

    tenth_result = await curriculum.list_chapters(str(subject.id), grade=10, user=teacher, session=db_session)
    ninth_result = await curriculum.list_chapters(str(subject.id), grade=9, user=teacher, session=db_session)

    assert [c.name for c in tenth_result.items] == ["10th chapter"]
    assert [c.name for c in ninth_result.items] == ["9th chapter"]


@pytest.mark.asyncio
async def test_a_chapter_with_no_grade_set_matches_every_grade(db_session):
    """NULL means "applies to any grade" (every chapter that predates this
    column), not "belongs to no grade" - it must not disappear once a
    caller starts passing grade."""
    teacher, subject = await _seed_teacher_and_subject(db_session)
    ungraded = Chapter(subject_id=subject.id, name="Ungraded legacy chapter", grade=None)
    db_session.add(ungraded)
    await db_session.flush()

    result = await curriculum.list_chapters(str(subject.id), grade=10, user=teacher, session=db_session)

    assert [c.name for c in result.items] == ["Ungraded legacy chapter"]


@pytest.mark.asyncio
async def test_omitting_grade_returns_every_chapter_unfiltered(db_session):
    """Existing callers that don't yet pass grade keep today's behavior."""
    teacher, subject = await _seed_teacher_and_subject(db_session)
    ninth_chapter = Chapter(subject_id=subject.id, name="9th chapter", grade=9)
    tenth_chapter = Chapter(subject_id=subject.id, name="10th chapter", grade=10)
    db_session.add_all([ninth_chapter, tenth_chapter])
    await db_session.flush()

    result = await curriculum.list_chapters(str(subject.id), user=teacher, session=db_session)

    assert {c.name for c in result.items} == {"9th chapter", "10th chapter"}


@pytest.mark.asyncio
async def test_a_subject_not_enabled_for_the_school_isnt_browsable(db_session):
    from shared.errors import NotFoundError

    teacher, _ = await _seed_teacher_and_subject(db_session)
    other = Subject(name=f"Never-assigned-{uuid.uuid4()}")
    db_session.add(other)
    await db_session.flush()
    chapter = Chapter(subject_id=other.id, name="Hidden")
    db_session.add(chapter)
    await db_session.flush()
    with pytest.raises(NotFoundError):
        await curriculum.list_chapters(str(other.id), grade=None, user=teacher, session=db_session)
    with pytest.raises(NotFoundError):
        await curriculum.list_topics(str(chapter.id), user=teacher, session=db_session)
