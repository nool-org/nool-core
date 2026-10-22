"""A teacher resets the password of a student in one of their own classes."""
import uuid

import pytest
from sqlalchemy import select

from shared.errors import ForbiddenError, NotFoundError

from src.api.routes import roster
from src.domain.models import Role, Subject, TeacherClassAssignment, User, UserStatus
from tests.test_admin_students import _seed_class, _seed_school, _seed_student


async def test_teacher_resets_own_students_password_only(db_session, monkeypatch):
    monkeypatch.setattr(roster, "reset_password", lambda *, firebase_uid, role: "NewTemp789")
    school = await _seed_school(db_session)
    mine, other = await _seed_class(db_session, school.id), await _seed_class(db_session, school.id)
    teacher = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                   role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
    db_session.add(teacher)
    await db_session.flush()
    subject = (await db_session.execute(select(Subject).limit(1))).scalar_one()
    db_session.add(TeacherClassAssignment(teacher_id=teacher.id, class_id=mine.id, subject_id=subject.id))
    student = await _seed_student(db_session, school.id, mine.id)
    stranger = await _seed_student(db_session, school.id, other.id)
    student.must_change_password = False
    await db_session.flush()

    out = await roster.reset_class_student_password(str(mine.id), str(student.id), user=teacher, session=db_session)
    assert out.temp_password == "NewTemp789"
    assert student.must_change_password is True

    with pytest.raises(NotFoundError):  # not in this class
        await roster.reset_class_student_password(str(mine.id), str(stranger.id), user=teacher, session=db_session)
    with pytest.raises(ForbiddenError):  # not this teacher's class
        await roster.reset_class_student_password(str(other.id), str(stranger.id), user=teacher, session=db_session)
