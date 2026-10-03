"""A teacher assigned to Science in a grade-10 class only ever sees grade-10
Science - the grade-9 "Science" subject (same name, own chapters) never
reaches them, and nothing lets it be attached to a grade-10 class, paper
or question bank."""

import uuid

import pytest

from shared.errors import ValidationError
from src.api.routes import curriculum, school_admin, school_question_bank
from src.api.schemas.school_admin import ClassAssignmentOut, UpdateClassAssignmentsIn
from src.domain.models import (
    Chapter, Role, School, SchoolClass, SchoolCurriculum, Subject, TeacherClassAssignment, Topic, User, UserStatus,
)
from src.services.subject_grades import ensure_subjects_fit_grade


async def _seed(db_session) -> dict:
    school = School(name=f"S-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
    name = f"Science-{uuid.uuid4()}"
    nine, ten = Subject(name=name, grade=9), Subject(name=name, grade=10)
    db_session.add_all([school, nine, ten])
    await db_session.flush()
    section = SchoolClass(school_id=school.id, grade=10, section="A")
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@x.com", display_name="T",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    admin = User(firebase_uid=f"a-{uuid.uuid4()}", email=f"{uuid.uuid4()}@x.com", display_name="A",
                 role=Role.SCHOOL_ADMIN, school_id=school.id, status=UserStatus.ACTIVE)
    # both grades' graphs number their first chapter CH01
    ch9 = Chapter(subject_id=nine.id, name="Matter in Our Surroundings", order_index=1, grade=9, kg_ref="CH01")
    ch10 = Chapter(subject_id=ten.id, name="Chemical Reactions and Equations", order_index=1, grade=10, kg_ref="CH01")
    db_session.add_all([section, teacher, admin, ch9, ch10])
    await db_session.flush()
    db_session.add_all([Topic(chapter_id=ch9.id, name="1.1 Physical nature of matter", kg_ref="CH01_T01"),
                        Topic(chapter_id=ch10.id, name="1.1 Chemical equations", kg_ref="CH01_T01"),
                        TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=ten.id),
                        *(SchoolCurriculum(school_id=school.id, subject_id=sub.id, enabled=True) for sub in (nine, ten))])
    await db_session.flush()
    return {"nine": nine, "ten": ten, "section": section, "teacher": teacher, "admin": admin, "ch10": ch10}


@pytest.mark.asyncio
async def test_a_grade_10_science_teacher_sees_only_grade_10_chapters_and_topics(db_session):
    s = await _seed(db_session)
    subjects = await curriculum.list_subjects_for_class(str(s["section"].id), user=s["teacher"], session=db_session)
    assert [(x.id, x.grade) for x in subjects.items] == [(str(s["ten"].id), 10)]

    chapters = await curriculum.list_chapters(subjects.items[0].id, grade=10, user=s["teacher"], session=db_session)
    assert [c.name for c in chapters.items] == ["Chemical Reactions and Equations"]
    topics = await curriculum.list_topics(chapters.items[0].id, user=s["teacher"], session=db_session)
    assert [t.name for t in topics.items] == ["1.1 Chemical equations"]


@pytest.mark.asyncio
async def test_a_grade_9_subject_cant_be_attached_to_grade_10(db_session):
    s = await _seed(db_session)
    with pytest.raises(ValidationError):   # teacher assignment
        await school_admin.update_class_assignments(
            str(s["section"].id),
            UpdateClassAssignmentsIn(assignments=[ClassAssignmentOut(teacher_id=str(s["teacher"].id),
                                                                     subject_id=str(s["nine"].id))]),
            user=s["admin"], session=db_session)
    with pytest.raises(ValidationError):   # the school's own question bank
        await school_question_bank.create_school_dataset(
            school_question_bank.CreateSchoolDatasetIn(name="Bank", subject_id=str(s["nine"].id), grade=10),
            user=s["admin"], session=db_session)
    with pytest.raises(ValidationError):   # the same check question papers use
        await ensure_subjects_fit_grade(db_session, [s["nine"].id], 10, what="a grade-10 paper")
    await ensure_subjects_fit_grade(db_session, [s["ten"].id], 10, what="a grade-10 paper")  # fine


@pytest.mark.asyncio
async def test_the_school_subject_list_tells_the_two_sciences_apart(db_session):
    s = await _seed(db_session)
    everything = await curriculum.list_subjects(grade=None, user=s["teacher"], session=db_session)
    grades = {x.grade for x in everything.items if x.id in (str(s["nine"].id), str(s["ten"].id))}
    assert grades == {9, 10}
    tenth = await curriculum.list_subjects(grade=10, user=s["teacher"], session=db_session)
    assert str(s["nine"].id) not in {x.id for x in tenth.items}


@pytest.mark.asyncio
async def test_a_school_cant_assign_a_teacher_to_a_subject_the_super_admin_never_enabled(db_session):
    s = await _seed(db_session)
    not_given = Subject(name=f"Sanskrit-{uuid.uuid4()}", grade=10)   # grade fits, but not enabled for the school
    db_session.add(not_given)
    await db_session.flush()
    with pytest.raises(ValidationError, match="Super Admin"):
        await school_admin.update_class_assignments(
            str(s["section"].id),
            UpdateClassAssignmentsIn(assignments=[ClassAssignmentOut(teacher_id=str(s["teacher"].id),
                                                                     subject_id=str(not_given.id))]),
            user=s["admin"], session=db_session)
    # an enabled, grade-10 subject is fine
    out = await school_admin.update_class_assignments(
        str(s["section"].id),
        UpdateClassAssignmentsIn(assignments=[ClassAssignmentOut(teacher_id=str(s["teacher"].id),
                                                                 subject_id=str(s["ten"].id))]),
        user=s["admin"], session=db_session)
    assert [a.subject_id for a in out.assignments] == [str(s["ten"].id)]


@pytest.mark.asyncio
async def test_a_class_with_teachers_or_tests_cant_change_grade(db_session):
    from shared.errors import ConflictError
    from src.api.routes import school_admin
    from src.api.schemas.school_admin import UpdateClassIn
    from tests.test_tenant_isolation import _school

    a = await _school(db_session)   # grade-10 section with a teacher assignment and a test
    with pytest.raises(ConflictError):
        await school_admin.update_school_class(str(a["section"].id), UpdateClassIn(grade=9, section="A"),
                                               user=a["admin"], session=db_session)
    renamed = await school_admin.update_school_class(str(a["section"].id), UpdateClassIn(grade=10, section="B"),
                                                     user=a["admin"], session=db_session)
    assert renamed.section == "B"
