"""create_paper regression (it always 500'd - the NOT NULL scalar columns
were only set after the flush that needed them already satisfied) and the
school-default Bloom distribution fallback that reuses the same code path.
"""

import uuid

import pytest

from src.api.routes import question_paper
from src.api.schemas.question_paper import CreateQuestionPaperIn
from src.domain.models import (
    Chapter, Role, School, SchoolClass, SchoolCurriculum, Subject, TeacherClassAssignment, User, UserStatus,
)


async def _seed_teacher_with_subject(db_session) -> tuple[User, Subject]:
    school = School(name="QP Test School", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()

    subject = Subject(name=f"Subject-{uuid.uuid4()}", grade=10)
    db_session.add(subject)
    await db_session.flush()

    teacher = User(
        firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    section = SchoolClass(school_id=school.id, grade=10, section="A")
    db_session.add_all([teacher, section])
    await db_session.flush()
    # teaches the subject in grade 10, which the Super Admin enabled for the school
    db_session.add_all([TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                        SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True)])
    await db_session.flush()
    return teacher, subject


async def test_create_paper_persists_required_fields_without_erroring(db_session):
    teacher, subject = await _seed_teacher_with_subject(db_session)

    result = await question_paper.create_paper(
        CreateQuestionPaperIn(
            name="Midterm", exam_type="UNIT_TEST", board="CBSE", grade=10,
            language="English", subject_id=str(subject.id),
        ),
        user=teacher, session=db_session,
    )

    assert result.name == "Midterm"
    assert result.subject_id == str(subject.id)


async def test_create_paper_falls_back_to_school_default_bloom_distribution(db_session):
    teacher, subject = await _seed_teacher_with_subject(db_session)
    school = await db_session.get(School, teacher.school_id)
    school.default_bloom_distribution = {"REMEMBER": 100}
    await db_session.flush()

    result = await question_paper.create_paper(
        CreateQuestionPaperIn(
            name="No Explicit Bloom", exam_type="UNIT_TEST", board="CBSE", grade=10,
            language="English", subject_id=str(subject.id),
        ),
        user=teacher, session=db_session,
    )

    assert len(result.bloom_distribution) == 1
    assert result.bloom_distribution[0].level == "REMEMBER"
    assert result.bloom_distribution[0].value == 100


async def test_create_paper_explicit_bloom_distribution_overrides_school_default(db_session):
    teacher, subject = await _seed_teacher_with_subject(db_session)
    school = await db_session.get(School, teacher.school_id)
    school.default_bloom_distribution = {"REMEMBER": 100}
    await db_session.flush()

    from src.api.schemas.bloom import BloomTarget

    result = await question_paper.create_paper(
        CreateQuestionPaperIn(
            name="Explicit Bloom", exam_type="UNIT_TEST", board="CBSE", grade=10,
            language="English", subject_id=str(subject.id),
            bloom_distribution=[BloomTarget(level="UNDERSTAND", value=100)],
        ),
        user=teacher, session=db_session,
    )

    assert len(result.bloom_distribution) == 1
    assert result.bloom_distribution[0].level == "UNDERSTAND"


async def test_blueprint_rules_and_status_guards(db_session):
    from shared.errors import ConflictError, ForbiddenError, ValidationError
    from src.domain.models import QuestionPaperStatus

    teacher, subject = await _seed_teacher_with_subject(db_session)
    other = Subject(name=f"Other-{uuid.uuid4()}", grade=10)
    db_session.add(other)
    await db_session.flush()
    foreign_chapter = Chapter(subject_id=other.id, name="Not this subject's")
    db_session.add(foreign_chapter)
    await db_session.flush()

    def body(**kw):
        return CreateQuestionPaperIn(**{"name": "P", "exam_type": "UNIT_TEST", "board": "CBSE", "language": "English",
                                        "grade": 10, "subject_id": str(subject.id), **kw})

    with pytest.raises(ForbiddenError):   # doesn't teach grade 9
        await question_paper.create_paper(body(grade=9), user=teacher, session=db_session)
    with pytest.raises(ValidationError):
        await question_paper.create_paper(body(chapter_ids=[str(foreign_chapter.id)]), user=teacher, session=db_session)

    paper = await question_paper.create_paper(body(), user=teacher, session=db_session)
    with pytest.raises(ConflictError):    # a DRAFT can't be finalized
        await question_paper.finalize_paper(paper.id, user=teacher, session=db_session)
    from src.domain.models import QuestionPaper
    (await db_session.get(QuestionPaper, paper.id)).status = QuestionPaperStatus.FINALIZED
    await db_session.flush()
    with pytest.raises(ConflictError):
        await question_paper.update_paper(paper.id, body(name="changed"), user=teacher, session=db_session)
