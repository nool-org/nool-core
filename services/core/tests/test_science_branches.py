"""Science split across teachers: a teacher assigned one branch sees only its
chapters (plus untagged ones); a teacher with no branch sees them all."""
import uuid

from src.api.routes import curriculum
from src.domain.models import Chapter, Role, SchoolCurriculum, Subject, TeacherClassAssignment, User, UserStatus
from tests.test_admin_students import _seed_class, _seed_school


async def test_teacher_sees_only_their_branch(db_session):
    school = await _seed_school(db_session)
    school_class = await _seed_class(db_session, school.id, grade=10)
    subject = Subject(name=f"Science-{uuid.uuid4().hex[:6]}", grade=10)
    db_session.add(subject)
    await db_session.flush()
    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True))
    for i, (name, branch) in enumerate([("Electricity", "PHYSICS"), ("Acids", "CHEMISTRY"), ("Heredity", "BIOLOGY"),
                                        ("Untagged", None)]):
        db_session.add(Chapter(subject_id=subject.id, name=name, grade=10, order_index=i, branch=branch))

    async def teacher(branch):
        t = User(firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="T",
                 role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE)
        db_session.add(t)
        await db_session.flush()
        db_session.add(TeacherClassAssignment(teacher_id=t.id, class_id=school_class.id, subject_id=subject.id,
                                              branch=branch))
        await db_session.flush()
        out = await curriculum.list_chapters(str(subject.id), grade=10, user=t, session=db_session)
        return [c.name for c in out.items]

    assert await teacher("PHYSICS") == ["Electricity", "Untagged"]
    assert await teacher("BIOLOGY") == ["Heredity", "Untagged"]
    assert await teacher(None) == ["Electricity", "Acids", "Heredity", "Untagged"]
