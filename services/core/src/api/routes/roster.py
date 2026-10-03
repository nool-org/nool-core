from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ForbiddenError

from src.api.deps import get_db_session, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.roster import RosterStudentOut, SchoolClassOut
from src.domain.models import Role, SchoolClass, StudentProfile, TeacherClassAssignment, User
from src.repositories.lookups import get_class_in_school
from src.repositories.roster_repository import student_count as _student_count
from src.repositories.roster_repository import teaches_class as _teaches_class

router = APIRouter(prefix="/api/v1", tags=["roster"])


@router.get("/classes")
async def list_classes(
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolClassOut]:
    """Scoped to classes this teacher is actually assigned to (via
    TeacherClassAssignment) - the same join teacher_dashboard.py already
    uses - not merely "every class in the school."
    """
    result = await session.execute(
        select(SchoolClass)
        .join(TeacherClassAssignment, TeacherClassAssignment.class_id == SchoolClass.id)
        # school filter too: an assignment row must never surface another school's class
        .where(TeacherClassAssignment.teacher_id == user.id, SchoolClass.school_id == user.school_id)
        .distinct()
    )
    classes = result.scalars().all()
    items = [
        SchoolClassOut(
            id=str(c.id), grade=c.grade, section=c.section, student_count=await _student_count(session, c.id)
        )
        for c in classes
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/classes/{class_id}")
async def get_class(
    class_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolClassOut:
    school_class = await get_class_in_school(session, class_id, user.school_id)
    if not await _teaches_class(session, user.id, class_id):
        raise ForbiddenError("That class isn't one of yours.")
    return SchoolClassOut(
        id=str(school_class.id),
        grade=school_class.grade,
        section=school_class.section,
        student_count=await _student_count(session, school_class.id),
    )


@router.get("/classes/{class_id}/students")
async def list_class_students(
    class_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[RosterStudentOut]:
    await get_class_in_school(session, class_id, user.school_id)
    if not await _teaches_class(session, user.id, class_id):
        raise ForbiddenError("That class isn't one of yours.")
    result = await session.execute(
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .where(StudentProfile.class_id == class_id)
    )
    items = [
        RosterStudentOut(
            id=str(u.id), class_id=class_id, display_name=u.display_name, roll_number=sp.roll_number
        )
        for u, sp in result.all()
    ]
    return ListEnvelope(items=items, total=len(items))
