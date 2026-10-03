"""School-scoped "get or 404" lookups shared across route modules -
homework.py and voice_test.py both need "this row, but only if it's in
the caller's school." Factored out here rather than duplicated across
route modules.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

from src.domain.models import (
    Homework, Role, SchoolClass, StudentProfile, TestStatus, User, UserStatus, VoiceTest,
)


async def get_class_in_school(session: AsyncSession, class_id: str, school_id: UUID) -> SchoolClass:
    result = await session.execute(
        select(SchoolClass).where(SchoolClass.id == class_id, SchoolClass.school_id == school_id)
    )
    school_class = result.scalar_one_or_none()
    if school_class is None:
        raise NotFoundError(f'Class "{class_id}" was not found.')
    return school_class


async def get_test_in_school(session: AsyncSession, test_id: str, school_id: UUID) -> VoiceTest:
    result = await session.execute(
        select(VoiceTest)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .where(VoiceTest.id == test_id, SchoolClass.school_id == school_id)
    )
    test = result.scalar_one_or_none()
    if test is None:
        raise NotFoundError(f'No Test with id "{test_id}" in your school.')
    return test


async def get_ready_test_in_school(session: AsyncSession, test_id: str, school_id: UUID) -> VoiceTest:
    test = await get_test_in_school(session, test_id, school_id)
    if test.status != TestStatus.RESULTS_READY:
        raise ConflictError("Test status isn't RESULTS_READY yet.")
    return test


async def get_homework_in_school(session: AsyncSession, homework_id: str, school_id: UUID) -> Homework:
    result = await session.execute(
        select(Homework)
        .join(SchoolClass, SchoolClass.id == Homework.class_id)
        .where(Homework.id == homework_id, SchoolClass.school_id == school_id)
    )
    hw = result.scalar_one_or_none()
    if hw is None:
        raise NotFoundError(f'No Homework with id "{homework_id}" in your school.')
    return hw


async def ensure_students_in_class(session: AsyncSession, student_ids, class_id) -> list[UUID]:
    """Targeted students (tests, homework) must be enrolled in that class -
    never another class's or another school's. Returns them de-duplicated."""
    wanted = list(dict.fromkeys(str(s) for s in student_ids))
    if not wanted:
        return []
    enrolled = {
        str(row[0]) for row in (await session.execute(
            select(StudentProfile.user_id).where(
                StudentProfile.class_id == class_id, StudentProfile.user_id.in_(wanted))
        )).all()
    }
    if outsiders := [s for s in wanted if s not in enrolled]:
        raise ValidationError(f"Not students of this class: {', '.join(outsiders)}")
    return [UUID(s) for s in wanted]


async def ensure_teachers_of_school(session: AsyncSession, teacher_ids, school_id) -> None:
    """Class assignments may only name active teachers of the class's own school."""
    wanted = {str(t) for t in teacher_ids}
    if not wanted:
        return
    ok = {
        str(row[0]) for row in (await session.execute(
            select(User.id).where(
                User.id.in_(wanted), User.school_id == school_id, User.role == Role.TEACHER,
                User.status != UserStatus.DEACTIVATED)
        )).all()
    }
    if bad := wanted - ok:
        raise ValidationError(f"Not an active teacher of this school: {', '.join(sorted(bad))}")


async def _ensure_teaches(session: AsyncSession, user: User, class_id, subject_id) -> None:
    from src.repositories.roster_repository import teaches_class_subject  # avoid an import cycle

    if user.role == Role.TEACHER and not await teaches_class_subject(session, user.id, class_id, subject_id):
        raise ForbiddenError("You don't teach this subject in this class.")


async def get_homework_for_teacher(session: AsyncSession, homework_id: str, user: User) -> Homework:
    """A homework in the caller's school AND in a class + subject they teach -
    a teacher can't generate, edit or assign another teacher's class's homework."""
    hw = await get_homework_in_school(session, homework_id, user.school_id)
    test = await session.get(VoiceTest, hw.test_id)
    await _ensure_teaches(session, user, hw.class_id, test.subject_id if test else None)
    return hw


async def get_test_for_teacher(session: AsyncSession, test_id: str, user: User, *, ready: bool = False) -> VoiceTest:
    """Same as get_(ready_)test_in_school, but only for a class + subject the teacher teaches."""
    test = await (get_ready_test_in_school if ready else get_test_in_school)(session, test_id, user.school_id)
    await _ensure_teaches(session, user, test.class_id, test.subject_id)
    return test
