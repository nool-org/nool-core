import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError

from src.api.deps import get_db_session, require_role
from src.config.settings import get_settings
from src.api.schemas.bloom import BloomDistributionOut, UpdateBloomDistributionIn
from src.api.schemas.school_logo import SchoolLogoOut, UpdateSchoolLogoIn
from src.api.schemas.common import ListEnvelope
from src.api.schemas.reporting import (
    CreateReportConfigurationIn,
    DimensionSpecOut,
    MetricSpecOut,
    ReportConfigurationOut,
    ReportResultOut,
    RunReportIn,
    SharedReportOut,
)
from src.api.schemas.school_admin import (
    BulkImportResultOut,
    BulkRowResultOut,
    ClassAssignmentOut,
    CreateClassIn,
    CreateGradeIn,
    CreateStudentIn,
    CreateStudentOut,
    InviteTeacherIn,
    InviteTeacherOut,
    ResetPasswordOut,
    SchoolAdminClassOut,
    SchoolAdminMeOut,
    SchoolAnalyticsOut,
    SchoolCurriculumOut,
    SchoolDatasetOut,
    GradeSubjectsOut,
    SchoolGradeOut,
    SchoolStudentOut,
    SchoolSubscriptionOut,
    SchoolTeacherOut,
    SendCredentialsEmailIn,
    SendCredentialsEmailOut,
    SubjectToggle,
    UpdateClassAssignmentsIn,
    UpdateClassIn,
    UpdateClassStatusIn,
    UpdateGradeStatusIn,
    UpdateSchoolAdminMeIn,
    UpdateStudentIn,
    UpdateTeacherIn,
    UpdateTeacherStatusIn,
    UpgradeRequestIn,
    UpgradeRequestOut,
)
from src.domain.models import (
    AssistantMessage,
    AuditLog,
    Homework,
    ImportJobType,
    Plan,
    QuestionPaper,
    ReportConfiguration,
    ReportShare,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    SchoolGrade,
    StudentHomeworkProgress,
    StudentPoints,
    StudentProfile,
    StudentTestResult,
    Subject,
    Subscription,
    TeacherClassAssignment,
    UpgradeRequest,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestTargetStudent,
)
from src.services.subject_grades import (
    ensure_class_grade_change_allowed, ensure_subjects_enabled_for_school, ensure_subjects_fit_grade,
)
from src.repositories.lookups import ensure_teachers_of_school, get_class_in_school
from src.repositories.dataset_access import school_dataset_listing, subject_names
from src.repositories import audit_repository, import_job_repository
from src.repositories.roster_repository import (
    get_or_create_grade,
    grade_student_count,
    section_count,
    student_count,
)
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import (
    count_question_papers,
    count_students,
    count_teachers,
    count_tests,
)
from src.services import reporting
from src.services.bulk_import import BulkRowResult, parse_rows
from src.services.email import send_email
from src.services.school_analytics import compute_school_analytics
from src.services.user_provisioning import create_firebase_user, reset_password

router = APIRouter(prefix="/api/v1/school", tags=["school-admin"])

# See request_subscription_upgrade's docstring for why this is enforced
# against the audit log rather than a dedicated table or in-memory state.
UPGRADE_REQUEST_COOLDOWN = timedelta(hours=1)

# Kept as thin aliases (rather than a mass rename across this file) - the
# actual counting logic now lives in usage_repository, shared with
# GET /school/subscription's usage-vs-limit fields and with
# voice_test.py/question_paper.py's own limit checks.
_current_teacher_count = count_teachers
_current_student_count = count_students


async def _row_exists(session: AsyncSession, column, value) -> bool:
    """Existence pre-check used before a permanent delete (teacher/student/
    class), instead of attempting the delete and catching the resulting
    IntegrityError - the FK violation approach also works, but leaves the
    DB-level transaction aborted in a way this codebase's session-per-request
    model doesn't cleanly recover from mid-request, so this checks first
    and never lets the delete hit a real constraint violation.
    """
    result = await session.execute(select(column).where(column == value).limit(1))
    return result.first() is not None


def _teacher_out(t: User, class_ids: list[str]) -> SchoolTeacherOut:
    return SchoolTeacherOut(
        id=str(t.id), display_name=t.display_name, email=t.email,
        phone_number=t.phone_number, employee_id=t.employee_id,
        class_ids=class_ids, status=t.status, must_change_password=t.must_change_password,
    )


@router.get("/teachers")
async def list_teachers(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolTeacherOut]:
    result = await session.execute(
        select(User).where(User.school_id == user.school_id, User.role == Role.TEACHER)
    )
    teachers = result.scalars().all()
    items = []
    for t in teachers:
        assignments = await session.execute(
            select(TeacherClassAssignment.class_id).where(TeacherClassAssignment.teacher_id == t.id)
        )
        class_ids = list({str(row[0]) for row in assignments.all()})
        items.append(_teacher_out(t, class_ids))
    return ListEnvelope(items=items, total=len(items))


@router.post("/teachers/invite", status_code=201)
async def invite_teacher(
    body: InviteTeacherIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> InviteTeacherOut:
    plan = await get_active_plan(session, user.school_id)
    if plan is not None:
        current = await _current_teacher_count(session, user.school_id)
        if current >= plan.teacher_limit:
            raise ConflictError(
                f"Your plan allows up to {plan.teacher_limit} teachers - you already have {current}."
            )

    provisioned = create_firebase_user(email=body.email, display_name=body.display_name, role=Role.TEACHER)

    teacher = User(
        firebase_uid=provisioned.firebase_uid,
        email=body.email, display_name=body.display_name, role=Role.TEACHER,
        school_id=user.school_id, status=UserStatus.ACTIVE,
        phone_number=body.phone_number, employee_id=body.employee_id,
        must_change_password=True,
    )
    session.add(teacher)
    await session.flush()
    await audit_repository.record(
        session, actor_id=user.id, action="teacher.invited", target_type="user", target_id=str(teacher.id),
        detail=f"{teacher.display_name} ({teacher.email})",
    )
    await session.commit()
    await session.refresh(teacher)
    return InviteTeacherOut(id=str(teacher.id), status=teacher.status, temp_password=provisioned.temp_password)


@router.post("/teachers/bulk-invite", summary="Bulk-invite teachers from a .csv or .xlsx file")
async def bulk_invite_teachers(
    file: UploadFile = File(...),
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BulkImportResultOut:
    """Columns (header row, any order): displayName, email, phoneNumber
    (optional), employeeId (optional). Rejects the whole batch upfront if
    it would exceed the plan's teacherLimit; otherwise each row is
    provisioned independently, so one bad row doesn't block the rest.
    """
    content = await file.read()
    rows = parse_rows(file.filename or "upload", content)

    plan = await get_active_plan(session, user.school_id)
    if plan is not None:
        current = await _current_teacher_count(session, user.school_id)
        if current + len(rows) > plan.teacher_limit:
            raise ConflictError(
                f"Your plan allows up to {plan.teacher_limit} teachers - you have {current} and this "
                f"file would add {len(rows)}, {current + len(rows) - plan.teacher_limit} over the limit."
            )

    results: list[BulkRowResult] = []
    for index, row in enumerate(rows, start=2):  # row 1 is the header
        display_name = row.get("displayname", "")
        email = row.get("email", "")
        if not display_name or not email:
            results.append(BulkRowResult(row=index, status="error", error="displayName and email are required."))
            continue
        try:
            provisioned = create_firebase_user(email=email, display_name=display_name, role=Role.TEACHER)
            teacher = User(
                firebase_uid=provisioned.firebase_uid, email=email, display_name=display_name,
                role=Role.TEACHER, school_id=user.school_id, status=UserStatus.ACTIVE,
                phone_number=row.get("phonenumber") or None, employee_id=row.get("employeeid") or None,
                must_change_password=True,
            )
            session.add(teacher)
            await session.commit()
            results.append(
                BulkRowResult(row=index, status="created", email=email, temp_password=provisioned.temp_password)
            )
        except ConflictError as exc:
            await session.rollback()
            results.append(BulkRowResult(row=index, status="error", email=email, error=exc.message))
        except Exception as exc:  # noqa: BLE001 - one bad row must not fail the batch
            await session.rollback()
            results.append(BulkRowResult(row=index, status="error", email=email, error=str(exc)))

    created_count = sum(1 for r in results if r.status == "created")
    error_count = sum(1 for r in results if r.status == "error")
    # One summary row per batch, not one per teacher - a per-row audit
    # entry here would flood the log without adding anything the per-row
    # BulkRowResultOut (already shown to the caller) doesn't already say.
    await audit_repository.record(
        session, actor_id=user.id, action="teacher.bulk_invited", target_type="school",
        target_id=str(user.school_id),
        detail=f"{created_count} invited, {error_count} failed",
    )
    import_job_repository.record(
        session, school_id=user.school_id, initiated_by=user.id, job_type=ImportJobType.TEACHER_INVITE,
        filename=file.filename or "upload", row_count=len(rows),
        created_count=created_count, error_count=error_count,
    )
    await session.commit()
    return BulkImportResultOut(
        results=[BulkRowResultOut(**r.__dict__) for r in results],
        created_count=created_count,
        error_count=error_count,
    )


@router.patch("/teachers/{teacher_id}/status")
async def update_teacher_status(
    teacher_id: str,
    body: UpdateTeacherStatusIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolTeacherOut:
    result = await session.execute(
        select(User).where(User.id == teacher_id, User.school_id == user.school_id, User.role == Role.TEACHER)
    )
    teacher = result.scalar_one_or_none()
    if teacher is None:
        raise NotFoundError(f'No teacher with id "{teacher_id}" in your school.')
    teacher.status = body.status
    await audit_repository.record(
        session, actor_id=user.id, action="teacher.status.updated", target_type="user", target_id=teacher_id,
        detail=f"{teacher.display_name} -> {body.status.value}",
    )
    await session.commit()
    assignments = await session.execute(
        select(TeacherClassAssignment.class_id).where(TeacherClassAssignment.teacher_id == teacher.id)
    )
    return _teacher_out(teacher, list({str(row[0]) for row in assignments.all()}))


async def _get_teacher_in_school(session: AsyncSession, teacher_id: str, school_id) -> User:
    result = await session.execute(
        select(User).where(User.id == teacher_id, User.school_id == school_id, User.role == Role.TEACHER)
    )
    teacher = result.scalar_one_or_none()
    if teacher is None:
        raise NotFoundError(f'No teacher with id "{teacher_id}" in your school.')
    return teacher


@router.patch("/teachers/{teacher_id}", summary="Edit a teacher's profile")
async def update_teacher(
    teacher_id: str,
    body: UpdateTeacherIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolTeacherOut:
    teacher = await _get_teacher_in_school(session, teacher_id, user.school_id)
    if body.display_name is not None:
        teacher.display_name = body.display_name
    if body.phone_number is not None:
        teacher.phone_number = body.phone_number
    if body.employee_id is not None:
        teacher.employee_id = body.employee_id
    await audit_repository.record(
        session, actor_id=user.id, action="teacher.updated", target_type="user", target_id=teacher_id
    )
    await session.commit()
    assignments = await session.execute(
        select(TeacherClassAssignment.class_id).where(TeacherClassAssignment.teacher_id == teacher.id)
    )
    return _teacher_out(teacher, list({str(row[0]) for row in assignments.all()}))


@router.post("/teachers/{teacher_id}/reset-password", summary="Issue a new temp password")
async def reset_teacher_password(
    teacher_id: str,
    actor: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ResetPasswordOut:
    """The no-email recovery path: a locked-out teacher has no self-service
    way back in, so School Admin issues a fresh one-time temp password
    here - same one-time-reveal contract as invite.
    """
    teacher = await _get_teacher_in_school(session, teacher_id, actor.school_id)
    if teacher.firebase_uid is None:
        raise ConflictError("This teacher has no Firebase account to reset.")
    temp_password = reset_password(firebase_uid=teacher.firebase_uid, role=Role.TEACHER)
    teacher.must_change_password = True
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.password_reset", target_type="user", target_id=teacher_id
    )
    await session.commit()
    return ResetPasswordOut(temp_password=temp_password)


@router.get("/teachers/export", summary="Download the current teacher roster as CSV")
async def export_teachers(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    result = await session.execute(
        select(User).where(User.school_id == user.school_id, User.role == Role.TEACHER)
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["displayName", "email", "phoneNumber", "employeeId"])
    for t in result.scalars().all():
        writer.writerow([t.display_name, t.email, t.phone_number or "", t.employee_id or ""])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=teachers.csv"},
    )


@router.post("/teachers/{teacher_id}/send-credentials-email", summary="Email a temp password the admin has reviewed")
async def send_teacher_credentials_email(
    teacher_id: str,
    body: SendCredentialsEmailIn,
    actor: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SendCredentialsEmailOut:
    teacher = await _get_teacher_in_school(session, teacher_id, actor.school_id)
    send_email(to_email=teacher.email, subject=body.subject, message=body.message)
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.credentials_emailed", target_type="user", target_id=teacher_id
    )
    await session.commit()
    return SendCredentialsEmailOut(sent=True)


@router.delete("/teachers/{teacher_id}", summary="Permanently delete a teacher")
async def delete_teacher(
    teacher_id: str,
    actor: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Idempotent, like admin_catalog's delete_subject. Unlike status
    (deactivate), this can't be undone, so it blocks (via _row_exists,
    checked up front) rather than delete if the teacher has created real
    content (Tests, Question Papers, ...) - deactivate instead to revoke
    access without losing that content's history.
    """
    result = await session.execute(
        select(User).where(User.id == teacher_id, User.school_id == actor.school_id, User.role == Role.TEACHER)
    )
    teacher = result.scalar_one_or_none()
    if teacher is None:
        return {"deleted": True}

    has_content = await _row_exists(
        session, AssistantMessage.teacher_id, teacher.id
    ) or await _row_exists(session, QuestionPaper.created_by, teacher.id)
    if has_content:
        raise ConflictError(
            "This teacher has created Tests, Question Papers, or other content and can't be deleted. "
            "Deactivate them instead to revoke access."
        )

    assignments = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.teacher_id == teacher.id)
    )
    for row in assignments.scalars().all():
        await session.delete(row)
    await session.flush()  # no ORM relationship links these two tables, so explicit ordering is needed
    await session.delete(teacher)
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.deleted", target_type="user", target_id=teacher_id,
        detail=f"{teacher.display_name} ({teacher.email})",
    )
    await session.commit()
    return {"deleted": True}


def _student_out(u: User, sp: StudentProfile) -> SchoolStudentOut:
    return SchoolStudentOut(
        id=str(u.id), display_name=u.display_name, email=u.email, class_id=str(sp.class_id),
        roll_number=sp.roll_number, guardian_name=sp.guardian_name, guardian_phone=sp.guardian_phone,
        date_of_birth=sp.date_of_birth, status=u.status, must_change_password=u.must_change_password,
    )


@router.get("/students")
async def list_students(
    class_id: str | None = Query(default=None, alias="classId"),
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolStudentOut]:
    query = (
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(SchoolClass, SchoolClass.id == StudentProfile.class_id)
        .where(SchoolClass.school_id == user.school_id)
    )
    if class_id:
        query = query.where(StudentProfile.class_id == class_id)
    result = await session.execute(query)
    items = [_student_out(u, sp) for u, sp in result.all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/students", status_code=201, summary="Create a student")
async def create_student(
    body: CreateStudentIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> CreateStudentOut:
    plan = await get_active_plan(session, user.school_id)
    if plan is not None:
        current = await _current_student_count(session, user.school_id)
        if current >= plan.student_limit:
            raise ConflictError(
                f"Your plan allows up to {plan.student_limit} students - you already have {current}."
            )

    await get_class_in_school(session, body.class_id, user.school_id)  # never another school's class
    provisioned = create_firebase_user(email=body.email, display_name=body.display_name, role=Role.STUDENT)

    student_user = User(
        firebase_uid=provisioned.firebase_uid, email=body.email,
        display_name=body.display_name, role=Role.STUDENT, school_id=user.school_id,
        status=UserStatus.ACTIVE, must_change_password=True,
    )
    session.add(student_user)
    await session.flush()
    session.add(
        StudentProfile(
            user_id=student_user.id, class_id=body.class_id, roll_number=body.roll_number,
            guardian_name=body.guardian_name, guardian_phone=body.guardian_phone,
            date_of_birth=body.date_of_birth,
        )
    )
    await audit_repository.record(
        session, actor_id=user.id, action="student.created", target_type="user", target_id=str(student_user.id),
        detail=f"{student_user.display_name} ({student_user.email})",
    )
    await session.commit()
    return CreateStudentOut(
        id=str(student_user.id), display_name=student_user.display_name, email=student_user.email,
        class_id=body.class_id, roll_number=body.roll_number, status=student_user.status,
        temp_password=provisioned.temp_password,
    )


@router.post("/students/bulk-create", summary="Bulk-create students from a .csv or .xlsx file")
async def bulk_create_students(
    file: UploadFile = File(...),
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BulkImportResultOut:
    """Columns (header row, any order): displayName, email, classGrade,
    classSection, rollNumber, guardianName (optional), guardianPhone
    (optional), dateOfBirth (optional, YYYY-MM-DD). classGrade/classSection
    are resolved to this school's class per row (not a raw classId - a
    human filling a spreadsheet won't know internal UUIDs).
    """
    content = await file.read()
    rows = parse_rows(file.filename or "upload", content)

    plan = await get_active_plan(session, user.school_id)
    if plan is not None:
        current = await _current_student_count(session, user.school_id)
        if current + len(rows) > plan.student_limit:
            raise ConflictError(
                f"Your plan allows up to {plan.student_limit} students - you have {current} and this "
                f"file would add {len(rows)}, {current + len(rows) - plan.student_limit} over the limit."
            )

    classes_result = await session.execute(select(SchoolClass).where(SchoolClass.school_id == user.school_id))
    classes_by_key = {(c.grade, c.section.strip().lower()): c for c in classes_result.scalars().all()}

    results: list[BulkRowResult] = []
    for index, row in enumerate(rows, start=2):
        display_name = row.get("displayname", "")
        email = row.get("email", "")
        grade_raw = row.get("classgrade", "")
        section = row.get("classsection", "").strip().lower()
        roll_raw = row.get("rollnumber", "")

        if not display_name or not email or not grade_raw or not section or not roll_raw:
            results.append(
                BulkRowResult(
                    row=index, status="error", email=email or None,
                    error="displayName, email, classGrade, classSection, and rollNumber are required.",
                )
            )
            continue

        school_class = classes_by_key.get((_safe_int(grade_raw), section))
        if school_class is None:
            results.append(
                BulkRowResult(row=index, status="error", email=email, error=f"No class {grade_raw}-{section} in your school.")
            )
            continue

        # Parsed up front, before any Firebase/DB call - create_student's
        # single-record path gets this for free from Pydantic's date
        # field, but the bulk path only ever has raw CSV/XLSX strings.
        # Catching a bad value here (not by letting asyncpg reject it
        # during commit) avoids ever provisioning a Firebase account for a
        # row that was always going to fail, and avoids running the rest
        # of this row's DB writes against a session an earlier exception
        # left in a failed-transaction state.
        date_of_birth_raw = row.get("dateofbirth") or None
        date_of_birth = None
        if date_of_birth_raw:
            try:
                date_of_birth = datetime.strptime(date_of_birth_raw, "%Y-%m-%d").date()
            except ValueError:
                results.append(
                    BulkRowResult(
                        row=index, status="error", email=email,
                        error='dateOfBirth must be in YYYY-MM-DD format.',
                    )
                )
                continue

        try:
            provisioned = create_firebase_user(email=email, display_name=display_name, role=Role.STUDENT)
            student_user = User(
                firebase_uid=provisioned.firebase_uid, email=email, display_name=display_name,
                role=Role.STUDENT, school_id=user.school_id, status=UserStatus.ACTIVE,
                must_change_password=True,
            )
            session.add(student_user)
            await session.flush()
            session.add(
                StudentProfile(
                    user_id=student_user.id, class_id=school_class.id, roll_number=_safe_int(roll_raw),
                    guardian_name=row.get("guardianname") or None, guardian_phone=row.get("guardianphone") or None,
                    date_of_birth=date_of_birth,
                )
            )
            await session.commit()
            results.append(
                BulkRowResult(row=index, status="created", email=email, temp_password=provisioned.temp_password)
            )
        except ConflictError as exc:
            await session.rollback()
            results.append(BulkRowResult(row=index, status="error", email=email, error=exc.message))
        except Exception as exc:  # noqa: BLE001 - one bad row must not fail the batch
            await session.rollback()
            results.append(BulkRowResult(row=index, status="error", email=email, error=str(exc)))

    created_count = sum(1 for r in results if r.status == "created")
    error_count = sum(1 for r in results if r.status == "error")
    await audit_repository.record(
        session, actor_id=user.id, action="student.bulk_created", target_type="school",
        target_id=str(user.school_id),
        detail=f"{created_count} created, {error_count} failed",
    )
    import_job_repository.record(
        session, school_id=user.school_id, initiated_by=user.id, job_type=ImportJobType.STUDENT_CREATE,
        filename=file.filename or "upload", row_count=len(rows),
        created_count=created_count, error_count=error_count,
    )
    await session.commit()
    return BulkImportResultOut(
        results=[BulkRowResultOut(**r.__dict__) for r in results],
        created_count=created_count,
        error_count=error_count,
    )


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1


@router.patch("/students/{student_id}", summary="Reassign class / change status")
async def update_student(
    student_id: str,
    body: UpdateStudentIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolStudentOut:
    result = await session.execute(
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .where(User.id == student_id, User.school_id == user.school_id)
    )
    row = result.first()
    if row is None:
        raise NotFoundError(f'No student with id "{student_id}" in your school.')
    student_user, profile = row
    if body.class_id is not None:
        await get_class_in_school(session, body.class_id, user.school_id)  # never another school's class
        profile.class_id = body.class_id
    if body.status is not None:
        student_user.status = body.status
    if body.display_name is not None:
        student_user.display_name = body.display_name
    if body.guardian_name is not None:
        profile.guardian_name = body.guardian_name
    if body.guardian_phone is not None:
        profile.guardian_phone = body.guardian_phone
    if body.date_of_birth is not None:
        profile.date_of_birth = body.date_of_birth
    await audit_repository.record(
        session, actor_id=user.id, action="student.updated", target_type="user", target_id=student_id
    )
    await session.commit()
    return _student_out(student_user, profile)


@router.post("/students/{student_id}/reset-password", summary="Issue a new temp password")
async def reset_student_password(
    student_id: str,
    actor: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ResetPasswordOut:
    result = await session.execute(
        select(User).where(User.id == student_id, User.school_id == actor.school_id, User.role == Role.STUDENT)
    )
    student_user = result.scalar_one_or_none()
    if student_user is None:
        raise NotFoundError(f'No student with id "{student_id}" in your school.')
    if student_user.firebase_uid is None:
        raise ConflictError("This student has no Firebase account to reset.")
    temp_password = reset_password(firebase_uid=student_user.firebase_uid, role=Role.STUDENT)
    student_user.must_change_password = True
    await audit_repository.record(
        session, actor_id=actor.id, action="student.password_reset", target_type="user", target_id=student_id
    )
    await session.commit()
    return ResetPasswordOut(temp_password=temp_password)


@router.get("/students/export", summary="Download the current student roster as CSV")
async def export_students(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    result = await session.execute(
        select(User, StudentProfile, SchoolClass)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(SchoolClass, SchoolClass.id == StudentProfile.class_id)
        .where(SchoolClass.school_id == user.school_id)
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["displayName", "email", "classGrade", "classSection", "rollNumber", "guardianName", "guardianPhone", "dateOfBirth"]
    )
    for u, sp, c in result.all():
        writer.writerow(
            [
                u.display_name, u.email, c.grade, c.section, sp.roll_number,
                sp.guardian_name or "", sp.guardian_phone or "",
                sp.date_of_birth.isoformat() if sp.date_of_birth else "",
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )


@router.post("/students/{student_id}/send-credentials-email", summary="Email a temp password the admin has reviewed")
async def send_student_credentials_email(
    student_id: str,
    body: SendCredentialsEmailIn,
    actor: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SendCredentialsEmailOut:
    result = await session.execute(
        select(User).where(User.id == student_id, User.school_id == actor.school_id, User.role == Role.STUDENT)
    )
    student_user = result.scalar_one_or_none()
    if student_user is None:
        raise NotFoundError(f'No student with id "{student_id}" in your school.')
    send_email(to_email=student_user.email, subject=body.subject, message=body.message)
    await audit_repository.record(
        session, actor_id=actor.id, action="student.credentials_emailed", target_type="user", target_id=student_id
    )
    await session.commit()
    return SendCredentialsEmailOut(sent=True)


@router.delete("/students/{student_id}", summary="Permanently delete a student")
async def delete_student(
    student_id: str,
    actor: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Same idempotent/blocked-up-front shape as delete_teacher - real
    activity (test results, homework progress, leaderboard points) blocks
    the delete rather than being cascaded away.
    """
    result = await session.execute(
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .where(User.id == student_id, User.school_id == actor.school_id)
    )
    row = result.first()
    if row is None:
        return {"deleted": True}
    student_user, profile = row

    has_activity = (
        await _row_exists(session, StudentTestResult.student_id, student_user.id)
        or await _row_exists(session, StudentHomeworkProgress.student_id, student_user.id)
        or await _row_exists(session, StudentPoints.student_id, student_user.id)
        or await _row_exists(session, VoiceTestTargetStudent.student_id, student_user.id)
    )
    if has_activity:
        raise ConflictError(
            "This student has test results, homework progress, or other activity and can't be deleted. "
            "Deactivate them instead to revoke access."
        )

    await session.delete(profile)
    await session.flush()  # no ORM relationship links these two tables, so explicit ordering is needed
    await session.delete(student_user)
    await audit_repository.record(
        session, actor_id=actor.id, action="student.deleted", target_type="user", target_id=student_id,
        detail=f"{student_user.display_name} ({student_user.email})",
    )
    await session.commit()
    return {"deleted": True}


@router.get("/classes")
async def list_school_classes(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolAdminClassOut]:
    result = await session.execute(select(SchoolClass).where(SchoolClass.school_id == user.school_id))
    classes = result.scalars().all()
    items = []
    for c in classes:
        assignments = await session.execute(
            select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == c.id)
        )
        items.append(
            SchoolAdminClassOut(
                id=str(c.id), grade=c.grade, section=c.section,
                student_count=await student_count(session, c.id),
                assignments=[
                    ClassAssignmentOut(teacher_id=str(a.teacher_id), subject_id=str(a.subject_id))
                    for a in assignments.scalars().all()
                ],
                status=c.status,
            )
        )
    return ListEnvelope(items=items, total=len(items))


@router.post("/classes", status_code=201)
async def create_school_class(
    body: CreateClassIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    grade_row = await get_or_create_grade(session, user.school_id, body.grade)
    school_class = SchoolClass(
        school_id=user.school_id, grade=body.grade, section=body.section, grade_id=grade_row.id,
    )
    session.add(school_class)
    await session.flush()
    await audit_repository.record(
        session, actor_id=user.id, action="class.created", target_type="class", target_id=str(school_class.id),
        detail=f"Class {school_class.grade} · {school_class.section}",
    )
    await session.commit()
    await session.refresh(school_class)
    return SchoolAdminClassOut(
        id=str(school_class.id), grade=school_class.grade, section=school_class.section,
        student_count=0, assignments=[], status=school_class.status,
    )


@router.patch("/classes/{class_id}", summary="Edit a class's grade/section")
async def update_school_class(
    class_id: str,
    body: UpdateClassIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    result = await session.execute(
        select(SchoolClass).where(SchoolClass.id == class_id, SchoolClass.school_id == user.school_id)
    )
    school_class = result.scalar_one_or_none()
    if school_class is None:
        raise NotFoundError(f'No class with id "{class_id}" in your school.')
    old_label = f"Class {school_class.grade} · {school_class.section}"
    await ensure_class_grade_change_allowed(session, school_class, body.grade)
    if body.grade != school_class.grade:
        school_class.grade_id = (await get_or_create_grade(session, user.school_id, body.grade)).id
    school_class.grade = body.grade
    school_class.section = body.section
    await audit_repository.record(
        session, actor_id=user.id, action="class.updated", target_type="class", target_id=class_id,
        detail=f"{old_label} -> Class {body.grade} · {body.section}",
    )
    await session.commit()
    assignments = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == class_id)
    )
    return SchoolAdminClassOut(
        id=str(school_class.id), grade=school_class.grade, section=school_class.section,
        student_count=await student_count(session, school_class.id),
        assignments=[
            ClassAssignmentOut(teacher_id=str(a.teacher_id), subject_id=str(a.subject_id))
            for a in assignments.scalars().all()
        ],
        status=school_class.status,
    )


@router.patch("/classes/{class_id}/status")
async def update_school_class_status(
    class_id: str,
    body: UpdateClassStatusIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    result = await session.execute(
        select(SchoolClass).where(SchoolClass.id == class_id, SchoolClass.school_id == user.school_id)
    )
    school_class = result.scalar_one_or_none()
    if school_class is None:
        raise NotFoundError(f'No class with id "{class_id}" in your school.')
    school_class.status = body.status
    await audit_repository.record(
        session, actor_id=user.id, action="class.status.updated", target_type="class", target_id=class_id,
        detail=f"Class {school_class.grade} · {school_class.section} -> {body.status.value}",
    )
    await session.commit()
    assignments = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == class_id)
    )
    return SchoolAdminClassOut(
        id=str(school_class.id), grade=school_class.grade, section=school_class.section,
        student_count=await student_count(session, school_class.id),
        assignments=[
            ClassAssignmentOut(teacher_id=str(a.teacher_id), subject_id=str(a.subject_id))
            for a in assignments.scalars().all()
        ],
        status=school_class.status,
    )


@router.delete("/classes/{class_id}", summary="Permanently delete a class")
async def delete_school_class(
    class_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Blocks up front (rather than letting a delete fail partway through)
    when the class still has students, or has Tests/Homework recorded
    against it - deactivate instead to hide it without losing that history.
    """
    result = await session.execute(
        select(SchoolClass).where(SchoolClass.id == class_id, SchoolClass.school_id == user.school_id)
    )
    school_class = result.scalar_one_or_none()
    if school_class is None:
        return {"deleted": True}

    remaining_students = await student_count(session, school_class.id)
    if remaining_students > 0:
        raise ConflictError(
            f"This class still has {remaining_students} student(s). Move or remove them first."
        )
    has_activity = await _row_exists(session, VoiceTest.class_id, school_class.id) or await _row_exists(
        session, Homework.class_id, school_class.id
    )
    if has_activity:
        raise ConflictError(
            "This class has Tests, Homework, or other activity recorded against it and can't be "
            "deleted. Deactivate it instead."
        )

    assignments = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == class_id)
    )
    for row in assignments.scalars().all():
        await session.delete(row)
    await session.flush()  # no ORM relationship links these two tables, so explicit ordering is needed
    await session.delete(school_class)
    await audit_repository.record(
        session, actor_id=user.id, action="class.deleted", target_type="class", target_id=class_id,
        detail=f"Class {school_class.grade} · {school_class.section}",
    )
    await session.commit()
    return {"deleted": True}


async def _grade_out(session: AsyncSession, school_grade: SchoolGrade) -> SchoolGradeOut:
    return SchoolGradeOut(
        id=str(school_grade.id), grade=school_grade.grade,
        section_count=await section_count(session, school_grade.id),
        student_count=await grade_student_count(session, school_grade.id),
        status=school_grade.status,
    )


@router.get("/grades", summary="List this school's Classes (the grade level, above Sections)")
async def list_school_grades(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolGradeOut]:
    result = await session.execute(select(SchoolGrade).where(SchoolGrade.school_id == user.school_id))
    items = [await _grade_out(session, g) for g in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/grades", status_code=201, summary="Create a Class (e.g. \"Class 10\")")
async def create_school_grade(
    body: CreateGradeIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolGradeOut:
    existing = await session.execute(
        select(SchoolGrade.id).where(SchoolGrade.school_id == user.school_id, SchoolGrade.grade == body.grade)
    )
    if existing.first() is not None:
        raise ConflictError(f"Class {body.grade} already exists at your school.")
    school_grade = SchoolGrade(school_id=user.school_id, grade=body.grade)
    session.add(school_grade)
    await session.flush()
    await audit_repository.record(
        session, actor_id=user.id, action="grade.created", target_type="grade", target_id=str(school_grade.id),
        detail=f"Class {school_grade.grade}",
    )
    await session.commit()
    return await _grade_out(session, school_grade)


@router.patch("/grades/{grade_id}/status", summary="Activate/deactivate a Class")
async def update_school_grade_status(
    grade_id: str,
    body: UpdateGradeStatusIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolGradeOut:
    result = await session.execute(
        select(SchoolGrade).where(SchoolGrade.id == grade_id, SchoolGrade.school_id == user.school_id)
    )
    school_grade = result.scalar_one_or_none()
    if school_grade is None:
        raise NotFoundError(f'No Class with id "{grade_id}" in your school.')
    school_grade.status = body.status
    await audit_repository.record(
        session, actor_id=user.id, action="grade.status.updated", target_type="grade", target_id=grade_id,
        detail=f"Class {school_grade.grade} -> {body.status.value}",
    )
    await session.commit()
    return await _grade_out(session, school_grade)


@router.delete("/grades/{grade_id}", summary="Permanently delete a Class")
async def delete_school_grade(
    grade_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Idempotent, and blocked up front (same shape as delete_school_class)
    when any Section still belongs to this Class - move/delete those first.
    """
    result = await session.execute(
        select(SchoolGrade).where(SchoolGrade.id == grade_id, SchoolGrade.school_id == user.school_id)
    )
    school_grade = result.scalar_one_or_none()
    if school_grade is None:
        return {"deleted": True}

    remaining_sections = await section_count(session, school_grade.id)
    if remaining_sections > 0:
        raise ConflictError(
            f"This Class still has {remaining_sections} section(s). Move or remove them first."
        )
    await session.delete(school_grade)
    await audit_repository.record(
        session, actor_id=user.id, action="grade.deleted", target_type="grade", target_id=grade_id,
        detail=f"Class {school_grade.grade}",
    )
    await session.commit()
    return {"deleted": True}


async def _get_own_grade(session: AsyncSession, grade_id: str, school_id) -> SchoolGrade:
    result = await session.execute(
        select(SchoolGrade).where(SchoolGrade.id == grade_id, SchoolGrade.school_id == school_id)
    )
    school_grade = result.scalar_one_or_none()
    if school_grade is None:
        raise NotFoundError(f'No Class with id "{grade_id}" in your school.')
    return school_grade


@router.get("/grades/{grade_id}/subjects", summary="View which subjects a Class teaches")
async def get_grade_subjects(
    # Deliberately read-only: School Admin can see which subjects are
    # assigned to a Class but can no longer toggle them (that write
    # capability - PUT /grades/{grade_id}/subjects - was removed, not just
    # hidden in the UI). Assignment is now exclusively Super Admin/PM's job,
    # via the cross-tenant PUT /admin/grades/{id}/subjects - same reasoning
    # and same pattern as get_school_curriculum's write removal above.
    # School Admin's own remaining job here is roster: adding Sections
    # under a Class, not deciding what it teaches.
    grade_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> GradeSubjectsOut:
    school_grade = await _get_own_grade(session, grade_id, user.school_id)
    # exactly the subjects a teacher can be assigned to in this grade's classes
    subjects = await _enabled_subjects(session, user.school_id, grade=school_grade.grade)
    return GradeSubjectsOut(
        grade_id=grade_id,
        subjects=[SubjectToggle(id=str(s.id), name=s.name, grade=s.grade, enabled=True, grades=[s.grade])
                  for s in subjects],
    )


async def _enabled_subjects(session: AsyncSession, school_id, *, grade: int | None = None) -> list[Subject]:
    """Subjects a Super Admin enabled for the school (optionally of one grade)."""
    query = (select(Subject)
             .join(SchoolCurriculum, SchoolCurriculum.subject_id == Subject.id)
             .where(SchoolCurriculum.school_id == school_id, SchoolCurriculum.enabled.is_(True))
             .order_by(Subject.grade.nulls_last(), func.lower(Subject.name)))
    if grade is not None:
        query = query.where(Subject.grade == grade)
    return list((await session.execute(query)).scalars().all())


@router.put("/classes/{class_id}/assignments", summary="Assign teacher + subject")
async def update_class_assignments(
    class_id: str,
    body: UpdateClassAssignmentsIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    """Full replace of this class's teacher/subject pairings, same pattern
    as Question Paper's question-order PUT.
    """
    result = await session.execute(
        select(SchoolClass).where(SchoolClass.id == class_id, SchoolClass.school_id == user.school_id)
    )
    school_class = result.scalar_one_or_none()
    if school_class is None:
        raise NotFoundError(f'No class with id "{class_id}" in your school.')

    existing = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == class_id)
    )
    for row in existing.scalars().all():
        await session.delete(row)
    await session.flush()

    # a grade-10 class can only be taught grade-10 subjects (their chapters/topics)
    await ensure_subjects_fit_grade(session, [a.subject_id for a in body.assignments], school_class.grade,
                                    what=f"a grade-{school_class.grade} class")
    await ensure_subjects_enabled_for_school(session, [a.subject_id for a in body.assignments],
                                             school_class.school_id)
    await ensure_teachers_of_school(session, [a.teacher_id for a in body.assignments], school_class.school_id)
    for a in body.assignments:
        session.add(
            TeacherClassAssignment(teacher_id=a.teacher_id, class_id=class_id, subject_id=a.subject_id)
        )
    await audit_repository.record(
        session, actor_id=user.id, action="class.assignments.updated", target_type="class", target_id=class_id,
        detail=f"{len(body.assignments)} assignment{'s' if len(body.assignments) != 1 else ''}",
    )
    await session.commit()

    assignments = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == class_id)
    )
    return SchoolAdminClassOut(
        id=str(school_class.id), grade=school_class.grade, section=school_class.section,
        student_count=await student_count(session, school_class.id),
        assignments=[
            ClassAssignmentOut(teacher_id=str(a.teacher_id), subject_id=str(a.subject_id))
            for a in assignments.scalars().all()
        ],
        status=school_class.status,
    )


@router.get("/curriculum", summary="This school's assigned subjects (read-only)")
async def get_school_curriculum(
    # Deliberately read-only: School Admin can see which subjects are
    # assigned/enabled for their school but can no longer toggle them
    # (that write capability - PUT /curriculum - was removed, not just
    # hidden in the UI). Assignment is now exclusively Super Admin's job,
    # from either the cross-tenant PUT /admin/schools/{id}/curriculum or
    # the per-Class GradeSubject toggle.
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolCurriculumOut:
    school = await session.get(School, user.school_id)
    subjects = await _enabled_subjects(session, user.school_id)
    return SchoolCurriculumOut(
        board=school.board if school else "",
        subjects=[SubjectToggle(id=str(s.id), name=s.name, grade=s.grade, enabled=True,
                                grades=[s.grade] if s.grade is not None else [])
                  for s in subjects],
    )


@router.get("/datasets")
async def get_school_datasets(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolDatasetOut]:
    """What this school can use: the catalog datasets a Super Admin assigned
    (read-only here - schools can't assign themselves) and its own
    question banks (`owned`, managed under /school/datasets/{id}/...)."""
    listing = await school_dataset_listing(session, user.school_id, include_unassigned=False)
    names = await subject_names(session, [d for d, *_ in listing])
    items = [
        SchoolDatasetOut(
            id=str(d.id), name=d.name, question_count=d.question_count, description=d.description,
            enabled=assigned, restricted=d.restricted, owned=owned, subject_enabled=subject_on,
            subject_id=str(d.subject_id) if d.subject_id else None, subject_name=names.get(d.subject_id),
            grade=d.grade,
        )
        for d, assigned, owned, subject_on in listing
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/analytics")
async def get_school_analytics(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAnalyticsOut:
    return await compute_school_analytics(session, user.school_id)


@router.get("/curriculum/default-bloom-distribution")
async def get_default_bloom_distribution(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BloomDistributionOut:
    school = await session.get(School, user.school_id)
    return BloomDistributionOut(distribution=school.default_bloom_distribution)


@router.put("/curriculum/default-bloom-distribution")
async def update_default_bloom_distribution(
    body: UpdateBloomDistributionIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BloomDistributionOut:
    school = await session.get(School, user.school_id)
    school.default_bloom_distribution = {level.value: value for level, value in body.distribution.items()}
    await audit_repository.record(
        session, actor_id=user.id, action="school.bloom_distribution.updated",
        target_type="school", target_id=str(user.school_id),
    )
    await session.commit()
    await session.refresh(school)
    return BloomDistributionOut(distribution=school.default_bloom_distribution)


@router.get("/logo")
async def get_school_logo(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolLogoOut:
    school = await session.get(School, user.school_id)
    return SchoolLogoOut(logo_data_uri=school.logo_data_uri)


@router.put("/logo")
async def update_school_logo(
    body: UpdateSchoolLogoIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolLogoOut:
    school = await session.get(School, user.school_id)
    school.logo_data_uri = body.logo_data_uri
    await audit_repository.record(
        session, actor_id=user.id, action="school.logo_updated", target_type="school", target_id=str(user.school_id),
    )
    await session.commit()
    await session.refresh(school)
    return SchoolLogoOut(logo_data_uri=school.logo_data_uri)


@router.get("/subscription")
async def get_school_subscription(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolSubscriptionOut:
    result = await session.execute(select(Subscription).where(Subscription.school_id == user.school_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise NotFoundError("No subscription for your school.")
    plan = await session.get(Plan, sub.plan_id)
    return SchoolSubscriptionOut(
        school_id=str(sub.school_id), plan_id=str(sub.plan_id), status=sub.status, renews_at=sub.renews_at,
        plan_name=plan.name,
        teacher_count=await count_teachers(session, user.school_id), teacher_limit=plan.teacher_limit,
        student_count=await count_students(session, user.school_id), student_limit=plan.student_limit,
        test_count=await count_tests(session, user.school_id), test_limit=plan.test_limit,
        question_paper_count=await count_question_papers(session, user.school_id),
        question_paper_limit=plan.question_paper_limit,
    )


@router.post("/subscription/upgrade-request")
async def request_subscription_upgrade(
    body: UpgradeRequestIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> UpgradeRequestOut:
    """Self-serve "Request an upgrade" CTA on the Subscription page - the
    plan itself stays read-only for School Admin (see GET /subscription's
    docstring), this just opens a line to noolAI instead of leaving the
    admin with nowhere to click. Always records to the audit log, which is
    the durable record whether or not email is configured; the email to
    sales_email is a best-effort notification on top of that, so a blank
    SMTP/sales_email setup (e.g. local dev) never fails the request.

    Rate-limited to one request per school per UPGRADE_REQUEST_COOLDOWN -
    guards against a stray double-click or repeated-click spamming
    sales_email, using the audit log itself as the source of truth rather
    than a separate table or an in-memory counter (which wouldn't survive
    a restart or work across multiple app instances).
    """
    last_request = (
        await session.execute(
            select(AuditLog.created_at)
            .where(AuditLog.action == "subscription.upgrade_requested", AuditLog.target_id == str(user.school_id))
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_request is not None:
        elapsed = datetime.now(timezone.utc) - last_request
        if elapsed < UPGRADE_REQUEST_COOLDOWN:
            wait_minutes = max(1, round((UPGRADE_REQUEST_COOLDOWN - elapsed).total_seconds() / 60))
            raise ConflictError(
                f"You've already requested an upgrade recently - your account team will be in touch. "
                f"Try again in about {wait_minutes} minute{'s' if wait_minutes != 1 else ''}."
            )

    school = await session.get(School, user.school_id)
    result = await session.execute(select(Subscription).where(Subscription.school_id == user.school_id))
    sub = result.scalar_one_or_none()
    plan_name = None
    if sub is not None:
        plan = await session.get(Plan, sub.plan_id)
        plan_name = plan.name

    await audit_repository.record(
        session, actor_id=user.id, action="subscription.upgrade_requested",
        target_type="school", target_id=str(user.school_id),
        detail=body.message,
    )
    # The durable, queryable half of this request - see UpgradeRequest's
    # model docstring for why the audit-log row above isn't enough on its
    # own (no resolved/unresolved state for Super Admin's inbox to key off).
    session.add(UpgradeRequest(school_id=user.school_id, requested_by=user.id, message=body.message))
    await session.commit()

    settings = get_settings()
    emailed = False
    if settings.sales_email and settings.smtp_host and settings.smtp_from_email:
        note = f"\n\nAdmin's note: {body.message}" if body.message else ""
        send_email(
            to_email=settings.sales_email,
            subject=f"Upgrade request from {school.name}",
            message=(
                f"School: {school.name} ({user.school_id})\n"
                f"Requested by: {user.email}\n"
                f"Current plan: {plan_name or 'none'}{note}"
            ),
        )
        emailed = True

    return UpgradeRequestOut(recorded=True, emailed=emailed)


@router.get("/me", summary="The signed-in School Admin's own account")
async def get_school_admin_me(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminMeOut:
    school = await session.get(School, user.school_id)
    return SchoolAdminMeOut(
        id=str(user.id), display_name=user.display_name, email=user.email,
        phone_number=user.phone_number, school_name=school.name if school else "",
    )


@router.patch("/me", summary="Edit the signed-in School Admin's own profile")
async def update_school_admin_me(
    body: UpdateSchoolAdminMeIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminMeOut:
    """Self-service only, deliberately separate from PATCH
    /admin/school-admins/{id} (Super Admin editing *someone else's*
    School Admin account) - that route is require_role(SUPER_ADMIN) and
    can't authorize a School Admin acting on themselves.
    """
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.phone_number is not None:
        user.phone_number = body.phone_number
    await audit_repository.record(
        session, actor_id=user.id, action="school_admin.self_updated", target_type="user", target_id=str(user.id)
    )
    await session.commit()
    school = await session.get(School, user.school_id)
    return SchoolAdminMeOut(
        id=str(user.id), display_name=user.display_name, email=user.email,
        phone_number=user.phone_number, school_name=school.name if school else "",
    )


# --- Reporting (School Admin, own school only) ------------------------------
# See src/services/reporting.py's DIMENSIONS registry. Only SCHOOL-scoped
# dimensions (CLASS/SECTION/STUDENT) are exposed here - a School Admin has
# exactly one school, so the platform-wide SCHOOL/PLAN dimensions (Super
# Admin only) aren't relevant. school_id is always user.school_id, never
# taken from the request body - see run_school_report below.

@router.get("/reports/dimensions", summary="List available report dimensions/metrics for this school")
async def list_school_report_dimensions(
    _: User = Depends(require_role(Role.SCHOOL_ADMIN)),
) -> list[DimensionSpecOut]:
    return [
        DimensionSpecOut(
            key=spec.key, label=spec.label, scope=spec.scope,
            metrics=[MetricSpecOut(key=m.key, label=m.label) for m in spec.metrics],
        )
        for spec in reporting.DIMENSIONS.values()
        if spec.scope == "SCHOOL"
    ]


@router.post("/reports/run", summary="Run a report ad-hoc, without saving it")
async def run_school_report(
    body: RunReportIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ReportResultOut:
    rows = await reporting.run_report(
        session, dimension=body.dimension, metrics=body.metrics, filters=body.filters, school_id=user.school_id,
    )
    return ReportResultOut(dimension=body.dimension, metrics=body.metrics, rows=rows)


def _school_report_config_out(config: ReportConfiguration) -> ReportConfigurationOut:
    return ReportConfigurationOut(
        id=str(config.id), name=config.name, dimension=config.dimension, metrics=config.metrics,
        filters=config.filters, school_id=str(config.school_id) if config.school_id else None,
        created_at=config.created_at,
    )


@router.get("/reports/configs", summary="List this school's saved reports")
async def list_school_report_configs(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ReportConfigurationOut]:
    result = await session.execute(
        select(ReportConfiguration)
        .where(ReportConfiguration.school_id == user.school_id)
        .order_by(ReportConfiguration.created_at.desc())
    )
    items = [_school_report_config_out(c) for c in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/reports/configs", status_code=201, summary="Save a report for this school")
async def create_school_report_config(
    body: CreateReportConfigurationIn,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ReportConfigurationOut:
    reporting.validate_dimension_and_metrics(body.dimension, body.metrics)
    config = ReportConfiguration(
        owner_id=user.id, school_id=user.school_id, name=body.name,
        dimension=body.dimension, metrics=body.metrics, filters=body.filters,
    )
    session.add(config)
    await session.flush()
    await audit_repository.record(
        session, actor_id=user.id, action="report.created", target_type="report", target_id=str(config.id),
        detail=body.name,
    )
    await session.commit()
    return _school_report_config_out(config)


async def _get_own_school_report_config(session: AsyncSession, config_id: str, school_id) -> ReportConfiguration:
    result = await session.execute(
        select(ReportConfiguration).where(
            ReportConfiguration.id == config_id, ReportConfiguration.school_id == school_id
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise NotFoundError(f'No saved report with id "{config_id}" at your school.')
    return config


@router.delete("/reports/configs/{config_id}", summary="Delete a saved report")
async def delete_school_report_config(
    config_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    config = await _get_own_school_report_config(session, config_id, user.school_id)
    await session.delete(config)
    await audit_repository.record(
        session, actor_id=user.id, action="report.deleted", target_type="report", target_id=config_id,
    )
    await session.commit()
    return {"deleted": True}


@router.get("/reports/configs/{config_id}/export", summary="Export a saved report's current results as CSV")
async def export_school_report_config(
    config_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    config = await _get_own_school_report_config(session, config_id, user.school_id)
    rows = await reporting.run_report(
        session, dimension=config.dimension, metrics=config.metrics, filters=config.filters,
        school_id=user.school_id,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["label", *config.metrics])
    for row in rows:
        writer.writerow([row.get("label", ""), *[row.get(m, "") for m in config.metrics]])
    return Response(
        content=buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={config.name}.csv"},
    )


@router.get("/reports/shared", summary="Reports a Super Admin has shared with this school")
async def list_shared_reports(
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SharedReportOut]:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(ReportShare, ReportConfiguration)
        .join(ReportConfiguration, ReportConfiguration.id == ReportShare.report_configuration_id)
        .where(ReportShare.shared_with_school_id == user.school_id)
        .order_by(ReportShare.created_at.desc())
    )
    items = []
    for share, config in result.all():
        if share.expires_at is not None and share.expires_at < now:
            continue
        sharer = await session.get(User, share.shared_by)
        items.append(
            SharedReportOut(
                share_id=str(share.id), name=config.name, dimension=config.dimension, metrics=config.metrics,
                filters=config.filters, access_level=share.access_level,
                shared_by_name=sharer.display_name if sharer else "noolAI",
                expires_at=share.expires_at,
            )
        )
    return ListEnvelope(items=items, total=len(items))


async def _get_valid_share_for_school(session: AsyncSession, share_id: str, school_id) -> ReportShare:
    result = await session.execute(
        select(ReportShare).where(ReportShare.id == share_id, ReportShare.shared_with_school_id == school_id)
    )
    share = result.scalar_one_or_none()
    if share is None:
        raise NotFoundError(f'No shared report with id "{share_id}" for your school.')
    if share.expires_at is not None and share.expires_at < datetime.now(timezone.utc):
        raise NotFoundError("This shared report has expired.")
    return share


@router.get("/reports/shared/{share_id}/run", summary="Run a report shared with this school")
async def run_shared_report(
    share_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ReportResultOut:
    share = await _get_valid_share_for_school(session, share_id, user.school_id)
    config = await session.get(ReportConfiguration, share.report_configuration_id)
    if config is None:
        raise NotFoundError("The shared report no longer exists.")
    rows = await reporting.run_report(
        session, dimension=config.dimension, metrics=config.metrics, filters=config.filters,
        school_id=config.school_id,
    )
    return ReportResultOut(dimension=config.dimension, metrics=config.metrics, rows=rows)


@router.get("/reports/shared/{share_id}/export", summary="Export a report shared with this school, as CSV")
async def export_shared_report(
    share_id: str,
    user: User = Depends(require_role(Role.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    share = await _get_valid_share_for_school(session, share_id, user.school_id)
    if share.access_level != "VIEW_EXPORT":
        raise ForbiddenError("This report was shared as view-only and can't be exported.")
    config = await session.get(ReportConfiguration, share.report_configuration_id)
    if config is None:
        raise NotFoundError("The shared report no longer exists.")
    rows = await reporting.run_report(
        session, dimension=config.dimension, metrics=config.metrics, filters=config.filters,
        school_id=config.school_id,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["label", *config.metrics])
    for row in rows:
        writer.writerow([row.get("label", ""), *[row.get(m, "") for m in config.metrics]])
    return Response(
        content=buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={config.name}.csv"},
    )
