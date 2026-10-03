"""Cross-school / cross-teacher isolation (QA phase 1): nothing in one school
can be reached, targeted or joined from another, and a teacher only acts
on classes + subjects they teach."""

import uuid

import pytest

from shared.errors import ForbiddenError, NotFoundError, ValidationError
from src.api.routes import ai_assessor, homework, roster, school_admin
from src.api.schemas.ai_assessor import OpenSessionIn
from src.api.schemas.school_admin import ClassAssignmentOut, CreateStudentIn, UpdateClassAssignmentsIn, UpdateStudentIn
from src.domain.models import (
    AssignmentTargetMode, BloomLevel, Chapter, Homework, HomeworkDifficulty, HomeworkStatus, Role, School,
    SchoolClass, SchoolCurriculum, StudentProfile, Subject, TeacherClassAssignment, User, UserStatus, VoiceTest,
    VoiceTestTargetStudent,
)


def _user(role, school):
    return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@x.com", display_name=role.value,
                role=role, school_id=school.id, status=UserStatus.ACTIVE)


async def _school(db_session):
    """A school with one grade-10 section, an admin, a teacher of Science there,
    a student in it, and a test on that class."""
    school = School(name=f"S-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}", grade=10)
    db_session.add_all([school, subject])
    await db_session.flush()
    section = SchoolClass(school_id=school.id, grade=10, section="A")
    admin, teacher, student = _user(Role.SCHOOL_ADMIN, school), _user(Role.TEACHER, school), _user(Role.STUDENT, school)
    chapter = Chapter(subject_id=subject.id, name="Electricity", grade=10, order_index=11)
    db_session.add_all([section, admin, teacher, student, chapter])
    await db_session.flush()
    test = VoiceTest(class_id=section.id, subject_id=subject.id, chapter_id=chapter.id, duration_minutes=20,
                     completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS)
    db_session.add_all([
        StudentProfile(user_id=student.id, class_id=section.id, roll_number=uuid.uuid4().int % 100000),
        TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
        SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True), test,
    ])
    await db_session.flush()
    db_session.add(VoiceTestTargetStudent(test_id=test.id, student_id=student.id))
    await db_session.flush()
    return dict(school=school, subject=subject, section=section, admin=admin, teacher=teacher,
                student=student, test=test)


@pytest.mark.asyncio
async def test_a_student_cant_open_a_session_on_another_schools_test(db_session):
    a, b = await _school(db_session), await _school(db_session)
    body = lambda test: OpenSessionIn(test_id=str(test.id), context_label="x",  # noqa: E731
                                      bloom_levels=[BloomLevel.REMEMBER], duration_seconds=60)
    with pytest.raises(NotFoundError):
        await ai_assessor.open_session(body(a["test"]), app_user=b["student"], db=db_session)
    with pytest.raises(ForbiddenError):     # a teacher/admin can't "take" a test either
        await ai_assessor.open_session(body(a["test"]), app_user=a["teacher"], db=db_session)
    opened = await ai_assessor.open_session(body(a["test"]), app_user=a["student"], db=db_session)
    assert opened.session_id


@pytest.mark.asyncio
async def test_a_school_admin_cant_put_a_student_into_another_schools_class(db_session, monkeypatch):
    a, b = await _school(db_session), await _school(db_session)
    with pytest.raises(NotFoundError):
        await school_admin.update_student(str(a["student"].id), UpdateStudentIn(class_id=str(b["section"].id)),
                                          user=a["admin"], session=db_session)
    monkeypatch.setattr(school_admin, "create_firebase_user", lambda **kw: pytest.fail("must fail before provisioning"))
    with pytest.raises(NotFoundError):
        await school_admin.create_student(
            CreateStudentIn(display_name="X", email=f"{uuid.uuid4()}@x.com", class_id=str(b["section"].id), roll_number=7),
            user=a["admin"], session=db_session)


@pytest.mark.asyncio
async def test_class_assignments_only_take_this_schools_active_teachers(db_session):
    a, b = await _school(db_session), await _school(db_session)
    for teacher in (b["teacher"], a["student"]):   # another school's teacher, or not a teacher at all
        with pytest.raises(ValidationError):
            await school_admin.update_class_assignments(
                str(a["section"].id),
                UpdateClassAssignmentsIn(assignments=[ClassAssignmentOut(teacher_id=str(teacher.id),
                                                                         subject_id=str(a["subject"].id))]),
                user=a["admin"], session=db_session)
    # even a stale cross-school assignment row never shows the other school's class
    db_session.add(TeacherClassAssignment(teacher_id=b["teacher"].id, class_id=a["section"].id,
                                          subject_id=a["subject"].id))
    await db_session.flush()
    listed = await roster.list_classes(user=b["teacher"], session=db_session)
    assert str(a["section"].id) not in {c.id for c in listed.items}


@pytest.mark.asyncio
async def test_a_teacher_only_sees_and_acts_on_homework_of_classes_they_teach(db_session):
    a = await _school(db_session)
    other_teacher = _user(Role.TEACHER, a["school"])
    db_session.add(other_teacher)
    await db_session.flush()
    hw = Homework(test_id=a["test"].id, class_id=a["section"].id, gap_topic="Electricity", gap_mastery_percent=50,
                  total_questions=3, difficulty=HomeworkDifficulty.EASY, target_mode=AssignmentTargetMode.WHOLE_CLASS,
                  status=HomeworkStatus.REVIEW)
    db_session.add(hw)
    await db_session.flush()

    mine = await homework.list_homework(user=a["teacher"], session=db_session)
    theirs = await homework.list_homework(user=other_teacher, session=db_session)
    assert str(hw.id) in {h.id for h in mine.items} and str(hw.id) not in {h.id for h in theirs.items}
    with pytest.raises(ForbiddenError):
        await homework.get_homework(str(hw.id), user=other_teacher, session=db_session)
