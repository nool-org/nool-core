from types import SimpleNamespace
from typing import Annotated
import csv
import io

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from shared.errors import ConflictError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_role
from src.api.schemas.admin import (
    AuditLogEntryOut,
    CreatePlanIn,
    CreateSchoolIn,
    CreateSubscriptionIn,
    InviteSchoolAdminIn,
    InviteSchoolAdminOut,
    PlanOut,
    PlatformAnalyticsOut,
    ResetPasswordOut,
    SchoolAdminOut,
    SchoolBreakdownOut,
    SchoolOut,
    SeatsOut,
    SubscriptionOut,
    UpdatePlanIn,
    UpdateSchoolAdminIn,
    UpdateSchoolAdminStatusIn,
    UpdateSchoolIn,
    UpdateSchoolStatusIn,
    UpdateSubscriptionIn,
    UpdateUpgradeRequestStatusIn,
    UpgradeRequestSummaryOut,
)
from src.api.schemas.bloom import BloomDistributionOut, UpdateBloomDistributionIn
from src.api.schemas.school_logo import SchoolLogoOut, UpdateSchoolLogoIn
from src.api.schemas.common import ListEnvelope
from src.api.schemas.features import FeaturesOut, UpdateFeaturesIn
from src.api.schemas.import_job import ImportJobOut
from src.api.schemas.support_ticket import (
    CreateTicketCommentIn,
    SupportTicketOut,
    TicketCommentOut,
    UpdateTicketStatusIn,
)
from src.api.schemas.school_admin import (
    BulkImportResultOut,
    BulkRowResultOut,
    ClassAssignmentOut,
    CreateClassIn,
    CreateGradeIn,
    CreateSchoolSubjectIn,
    CreateStudentIn,
    CreateStudentOut,
    InviteTeacherIn,
    InviteTeacherOut,
    MasteryTrendPointOut,
    GradeSubjectsOut,
    SchoolAdminClassOut,
    SchoolAnalyticsOut,
    SchoolCurriculumOut,
    SchoolDatasetOut,
    SchoolGradeOut,
    SchoolStudentOut,
    SchoolTeacherOut,
    SendCredentialsEmailIn,
    SendCredentialsEmailOut,
    SubjectToggle,
    UpdateClassAssignmentsIn,
    UpdateClassIn,
    UpdateClassStatusIn,
    UpdateGradeStatusIn,
    UpdateGradeSubjectsIn,
    UpdateSchoolCurriculumIn,
    UpdateSchoolDatasetsIn,
    UpdateStudentIn,
    UpdateTeacherIn,
    UpdateTeacherStatusIn,
)
from src.api.schemas.data_explorer import (
    ColumnSpecOut,
    EntitySpecOut,
    ExplorerQueryIn,
    ExplorerQueryOut,
)
from src.api.schemas.reporting import (
    CreateReportConfigurationIn,
    CreateReportShareIn,
    DimensionSpecOut,
    MetricSpecOut,
    ReportConfigurationOut,
    ReportResultOut,
    ReportShareOut,
    RunReportIn,
)
from src.api.schemas.school_oversight import (
    SchoolAuditLogEntryOut,
    SchoolHomeworkOut,
    SchoolLeaderboardEntryOut,
    SchoolQuestionBankEntryOut,
    SchoolQuestionPaperOut,
    SchoolChallengeOut,
    SchoolVoiceTestOut,
)
from src.domain.models import (
    AssistantMessage,
    AuditLog,
    Chapter,
    Dataset,
    DatasetQuestion,
    GradeSubject,
    Homework,
    HomeworkQuestion,
    ImportJob,
    ImportJobType,
    Plan,
    QuestionPaper,
    QuestionPaperQuestion,
    QuestionPaperTopic,
    ReportConfiguration,
    ReportShare,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    SchoolGrade,
    SchoolStatus,
    StudentHomeworkProgress,
    StudentPoints,
    StudentProfile,
    StudentTestResult,
    Subject,
    SubjectGrade,
    Subscription,
    SupportTicket,
    SupportTicketComment,
    TeacherClassAssignment,
    TicketStatus,
    Topic,
    UpgradeRequest,
    UpgradeRequestStatus,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestTargetStudent,
)
from src.services.subject_grades import (
    ensure_class_grade_change_allowed, ensure_subjects_enabled_for_school, ensure_subjects_fit_grade,
)
from src.repositories.lookups import ensure_teachers_of_school, get_class_in_school
from src.api.routes import school_oversight
from src.api.routes.school_oversight import question_bank_entries
from src.repositories.dataset_access import assign_catalog_datasets, school_dataset_listing, subject_names
from src.repositories import audit_repository
from src.repositories.roster_repository import (
    get_or_create_grade,
    grade_student_count,
    section_count,
    student_count,
)
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import count_students, count_teachers
from src.services import data_explorer, reporting
from src.services.subject_grades import (
    add_subject_grades,
    grades_by_subject,
    picker_subjects_for_grade,
    picker_subjects_for_school,
    validate_new_grants_for_grade,
    validate_new_grants_have_any_grade,
)
from src.services.bulk_import import BulkRowResult, parse_rows
from src.services.email import send_email
from src.services.school_analytics import compute_school_analytics
from src.services.user_provisioning import create_firebase_user, reset_password

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


async def _row_exists(session: AsyncSession, column, value) -> bool:
    """Same existence pre-check as school_admin.py's `_row_exists` (small,
    deliberate duplication - see this file's `_get_school` vs.
    school_admin.py's own school lookup for the existing precedent), used
    before delete_teacher_as_admin's permanent delete.
    """
    result = await session.execute(select(column).where(column == value).limit(1))
    return result.first() is not None


def _teacher_out(t: User, class_ids: list[str]) -> SchoolTeacherOut:
    return SchoolTeacherOut(
        id=str(t.id), display_name=t.display_name, email=t.email,
        phone_number=t.phone_number, employee_id=t.employee_id,
        class_ids=class_ids, status=t.status, must_change_password=t.must_change_password,
    )


def _student_out(u: User, sp: StudentProfile) -> SchoolStudentOut:
    return SchoolStudentOut(
        id=str(u.id), display_name=u.display_name, email=u.email, class_id=str(sp.class_id),
        roll_number=sp.roll_number, guardian_name=sp.guardian_name, guardian_phone=sp.guardian_phone,
        date_of_birth=sp.date_of_birth, status=u.status, must_change_password=u.must_change_password,
    )


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1


async def _counts_for_school(session: AsyncSession, school_id) -> tuple[int, int]:
    teachers = await session.execute(
        select(func.count()).select_from(User).where(User.school_id == school_id, User.role == Role.TEACHER)
    )
    students = await session.execute(
        select(func.count()).select_from(User).where(User.school_id == school_id, User.role == Role.STUDENT)
    )
    return teachers.scalar_one(), students.scalar_one()


async def _plan_id_for_school(session: AsyncSession, school_id) -> str | None:
    """SchoolOut.planId is always resolved via the school's Subscription
    row - see School.plan_id's deprecation note in
    src/domain/models/foundation.py.
    """
    result = await session.execute(select(Subscription.plan_id).where(Subscription.school_id == school_id))
    plan_id = result.scalar_one_or_none()
    return str(plan_id) if plan_id else None


async def _serialize_school(session: AsyncSession, school: School) -> SchoolOut:
    teacher_count, student_count = await _counts_for_school(session, school.id)
    return SchoolOut(
        id=str(school.id),
        name=school.name,
        board=school.board,
        city=school.city,
        address=school.address,
        pincode=school.pincode,
        contact_email=school.contact_email,
        contact_phone=school.contact_phone,
        principal_name=school.principal_name,
        status=school.status,
        plan_id=await _plan_id_for_school(session, school.id),
        teacher_count=teacher_count,
        student_count=student_count,
        created_at=school.created_at,
    )


@router.get("/schools")
async def list_schools(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolOut]:
    result = await session.execute(select(School))
    items = [await _serialize_school(session, s) for s in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


async def _get_school(session: AsyncSession, school_id: str) -> School:
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if school is None:
        raise NotFoundError(f'No school with id "{school_id}".')
    return school


@router.get("/schools/{school_id}")
async def get_school(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolOut:
    school = await _get_school(session, school_id)
    return await _serialize_school(session, school)


@router.post("/schools", status_code=201, summary="Onboard a school")
async def create_school(
    body: CreateSchoolIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolOut:
    await _assignable_plan(session, body.plan_id)
    school = School(
        name=body.name,
        board=body.board,
        city=body.city,
        contact_email=body.contact_email,
        address=body.address,
        pincode=body.pincode,
        contact_phone=body.contact_phone,
        principal_name=body.principal_name,
        plan_id=body.plan_id,
        status=SchoolStatus.ONBOARDING,
    )
    session.add(school)
    await session.flush()

    # Onboarding always creates the initial subscription in the same
    # transaction - previously this only stamped School.plan_id, leaving
    # schools with no real Subscription row (the "404 on subscription"
    # gap this session hit against a manually-seeded school).
    session.add(
        Subscription(
            school_id=school.id,
            plan_id=body.plan_id,
            renews_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    await audit_repository.record(
        session, actor_id=actor.id, action="school.onboarded", target_type="school", target_id=str(school.id)
    )
    await session.commit()
    await session.refresh(school)
    return await _serialize_school(session, school)


@router.patch("/schools/{school_id}/status")
async def update_school_status(
    school_id: str,
    body: UpdateSchoolStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolOut:
    """Suspending a school cuts off API access for every teacher/student/
    School Admin under it - enforced by require_role's school-scoped
    dependents rejecting anything under a non-ACTIVE school, not just
    hidden in a UI.
    """
    school = await _get_school(session, school_id)
    school.status = body.status
    await audit_repository.record(
        session, actor_id=actor.id, action="school.status.updated", target_type="school", target_id=school_id
    )
    await session.commit()
    await session.refresh(school)
    return await _serialize_school(session, school)


@router.patch("/schools/{school_id}", summary="Edit a school's profile")
async def update_school(
    school_id: str,
    body: UpdateSchoolIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolOut:
    school = await _get_school(session, school_id)
    for field in ("name", "board", "city", "contact_email", "address", "pincode", "contact_phone", "principal_name"):
        value = getattr(body, field)
        if value is not None:
            setattr(school, field, value)
    await audit_repository.record(
        session, actor_id=actor.id, action="school.updated", target_type="school", target_id=school_id
    )
    await session.commit()
    await session.refresh(school)
    return await _serialize_school(session, school)


@router.get("/schools/{school_id}/default-bloom-distribution")
async def get_school_default_bloom_distribution(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BloomDistributionOut:
    school = await _get_school(session, school_id)
    return BloomDistributionOut(distribution=school.default_bloom_distribution)


@router.put("/schools/{school_id}/default-bloom-distribution", summary="Override a school's default Bloom mix")
async def update_school_default_bloom_distribution(
    school_id: str,
    body: UpdateBloomDistributionIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BloomDistributionOut:
    school = await _get_school(session, school_id)
    school.default_bloom_distribution = {level.value: value for level, value in body.distribution.items()}
    await audit_repository.record(
        session, actor_id=actor.id, action="school.bloom_distribution.updated",
        target_type="school", target_id=school_id,
    )
    await session.commit()
    await session.refresh(school)
    return BloomDistributionOut(distribution=school.default_bloom_distribution)


@router.get("/schools/{school_id}/logo")
async def get_school_logo(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolLogoOut:
    school = await _get_school(session, school_id)
    return SchoolLogoOut(logo_data_uri=school.logo_data_uri)


@router.put("/schools/{school_id}/logo", summary="Set/clear a school's logo")
async def update_school_logo(
    school_id: str,
    body: UpdateSchoolLogoIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolLogoOut:
    school = await _get_school(session, school_id)
    school.logo_data_uri = body.logo_data_uri
    await audit_repository.record(
        session, actor_id=actor.id, action="school.logo_updated", target_type="school", target_id=school_id,
    )
    await session.commit()
    await session.refresh(school)
    return SchoolLogoOut(logo_data_uri=school.logo_data_uri)


def _features_label(features: list[str] | None) -> str:
    return "all features" if features is None else (", ".join(features) or "no features")


def _plan_out(p: Plan) -> PlanOut:
    return PlanOut(
        id=str(p.id), name=p.name, price_label=p.price_label,
        teacher_limit=p.teacher_limit, student_limit=p.student_limit,
        test_limit=p.test_limit, question_paper_limit=p.question_paper_limit,
        spoken_days_per_week=p.spoken_days_per_week, extra_challenges_per_month=p.extra_challenges_per_month or 0,
        enabled_features=p.enabled_features, active=p.active,
    )


async def _get_plan(session: AsyncSession, plan_id: str) -> Plan:
    result = await session.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f'No plan with id "{plan_id}".')
    return plan


@router.get("/plans", summary="Subscription plan catalog")
async def list_plans(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[PlanOut]:
    result = await session.execute(select(Plan))
    items = [_plan_out(p) for p in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


async def _assignable_plan(session: AsyncSession, plan_id) -> Plan:
    plan = await _get_plan(session, plan_id)
    if not plan.active:
        raise ValidationError(f'The "{plan.name}" plan is inactive - pick an active plan.')
    return plan


async def _plan_name_taken(session: AsyncSession, name: str, *, exclude_plan_id: str | None = None) -> bool:
    query = select(Plan.id).where(Plan.name == name)
    if exclude_plan_id is not None:
        query = query.where(Plan.id != exclude_plan_id)
    result = await session.execute(query)
    return result.first() is not None


@router.post("/plans", status_code=201, summary="Create a plan")
async def create_plan(
    body: CreatePlanIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> PlanOut:
    if await _plan_name_taken(session, body.name):
        raise ConflictError(f'A plan named "{body.name}" already exists.')

    plan = Plan(
        name=body.name, price_label=body.price_label,
        teacher_limit=body.teacher_limit, student_limit=body.student_limit,
        test_limit=body.test_limit, question_paper_limit=body.question_paper_limit,
        spoken_days_per_week=body.spoken_days_per_week, extra_challenges_per_month=body.extra_challenges_per_month,
    )
    session.add(plan)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="plan.created", target_type="plan", target_id=str(plan.id)
    )
    await session.commit()
    await session.refresh(plan)
    return _plan_out(plan)


@router.patch("/plans/{plan_id}", summary="Edit a plan")
async def update_plan(
    plan_id: str,
    body: UpdatePlanIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> PlanOut:
    result = await session.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f'No plan with id "{plan_id}".')

    if body.name is not None and await _plan_name_taken(session, body.name, exclude_plan_id=plan_id):
        raise ConflictError(f'A plan named "{body.name}" already exists.')

    for field in ("name", "price_label", "teacher_limit", "student_limit", "active", "extra_challenges_per_month"):
        value = getattr(body, field)
        if value is not None:
            setattr(plan, field, value)
    # these may be unlimited: an explicit null clears the limit, omitted keeps it
    for field in ("test_limit", "question_paper_limit", "spoken_days_per_week"):
        if field in body.model_fields_set:
            setattr(plan, field, getattr(body, field))

    await audit_repository.record(
        session, actor_id=actor.id, action="plan.updated", target_type="plan", target_id=plan_id
    )
    await session.commit()
    await session.refresh(plan)
    return _plan_out(plan)


@router.get("/plans/{plan_id}/features", summary="A plan's bundled features")
async def get_plan_features(
    plan_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> FeaturesOut:
    plan = await _get_plan(session, plan_id)
    return FeaturesOut(enabled_features=plan.enabled_features)


@router.put("/plans/{plan_id}/features", summary="Set which features this plan's tier bundles at all")
async def update_plan_features(
    plan_id: str,
    body: UpdateFeaturesIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> FeaturesOut:
    plan = await _get_plan(session, plan_id)
    before = plan.enabled_features
    after = [f.value for f in body.enabled_features] if body.enabled_features is not None else None
    plan.enabled_features = after
    await audit_repository.record(
        session, actor_id=actor.id, action="plan.features_updated", target_type="plan", target_id=plan_id,
        detail=f"{_features_label(before)} -> {_features_label(after)}",
    )
    await session.commit()
    await session.refresh(plan)
    return FeaturesOut(enabled_features=plan.enabled_features)


async def _serialize_subscription(session: AsyncSession, sub: Subscription) -> SubscriptionOut:
    plan_result = await session.execute(select(Plan).where(Plan.id == sub.plan_id))
    plan = plan_result.scalar_one_or_none()
    teacher_count, student_count = await _counts_for_school(session, sub.school_id)
    return SubscriptionOut(
        school_id=str(sub.school_id),
        plan_id=str(sub.plan_id),
        status=sub.status,
        renews_at=sub.renews_at,
        discount_percent=sub.discount_percent,
        seats_used=SeatsOut(teachers=teacher_count, students=student_count),
        seats_limit=SeatsOut(
            teachers=plan.teacher_limit if plan else 0, students=plan.student_limit if plan else 0
        ),
    )


@router.get("/schools/{school_id}/subscription")
async def get_school_subscription(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubscriptionOut:
    result = await session.execute(select(Subscription).where(Subscription.school_id == school_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise NotFoundError(f'No subscription for school "{school_id}".')
    return await _serialize_subscription(session, sub)


@router.post("/schools/{school_id}/subscription", status_code=201, summary="Create a subscription")
async def create_school_subscription(
    school_id: str,
    body: CreateSubscriptionIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubscriptionOut:
    """Covers schools that predate onboarding auto-creating a subscription,
    or that need one re-created. 409 if the school already has one - use
    PUT to change an existing subscription instead.
    """
    await _get_school(session, school_id)
    existing = await session.execute(select(Subscription).where(Subscription.school_id == school_id))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("This school already has a subscription - use PUT to change it instead.")

    await _assignable_plan(session, body.plan_id)

    sub = Subscription(
        school_id=school_id,
        plan_id=body.plan_id,
        status=body.status,
        renews_at=body.renews_at or (datetime.now(timezone.utc) + timedelta(days=30)),
        discount_percent=body.discount_percent,
    )
    session.add(sub)
    await audit_repository.record(
        session, actor_id=actor.id, action="subscription.created", target_type="school", target_id=school_id
    )
    await session.commit()
    await session.refresh(sub)
    return await _serialize_subscription(session, sub)


@router.put("/schools/{school_id}/subscription", summary="Change a subscription")
async def update_school_subscription(
    school_id: str,
    body: UpdateSubscriptionIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubscriptionOut:
    result = await session.execute(select(Subscription).where(Subscription.school_id == school_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise NotFoundError(f'No subscription for school "{school_id}".')

    if body.plan_id is not None:
        plan = await _assignable_plan(session, body.plan_id)
        teacher_count, student_count = await _counts_for_school(session, school_id)
        if teacher_count > plan.teacher_limit or student_count > plan.student_limit:
            raise ConflictError("New plan's limit is below the school's current seat usage.")
        sub.plan_id = body.plan_id

    if body.status is not None:
        sub.status = body.status
    if body.renews_at is not None:
        sub.renews_at = body.renews_at
    if body.discount_percent is not None:
        sub.discount_percent = body.discount_percent

    await audit_repository.record(
        session, actor_id=actor.id, action="subscription.updated", target_type="school", target_id=school_id
    )
    await session.commit()
    await session.refresh(sub)
    return await _serialize_subscription(session, sub)


async def _mastery_avg_for_school(session: AsyncSession, school_id) -> int:
    row = await session.execute(
        select(func.avg(StudentTestResult.mastery_percent))
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .join(SchoolClass, SchoolClass.id == VoiceTest.class_id)
        .where(SchoolClass.school_id == school_id)
    )
    avg = row.scalar_one_or_none()
    return round(avg) if avg is not None else 0


@router.get("/analytics/platform")
async def get_platform_analytics(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> PlatformAnalyticsOut:
    schools_result = await session.execute(select(School))
    schools = schools_result.scalars().all()
    active_schools = [s for s in schools if s.status == SchoolStatus.ACTIVE]

    total_teachers = await session.execute(select(func.count()).select_from(User).where(User.role == Role.TEACHER))
    total_students = await session.execute(select(func.count()).select_from(User).where(User.role == Role.STUDENT))

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    tests_this_month_row = await session.execute(
        select(func.count()).select_from(VoiceTest).where(VoiceTest.created_at >= month_start)
    )
    tests_this_month = tests_this_month_row.scalar_one()

    # All-time completion rate: Homework.assigned_count/completed_count are
    # running totals already maintained on the row itself (see
    # domain/models/homework.py), not something this endpoint recomputes
    # from StudentHomeworkProgress - simplest honest number available
    # without inventing a "this month" homework-assignment window.
    homework_totals = await session.execute(select(func.sum(Homework.assigned_count), func.sum(Homework.completed_count)))
    assigned_total, completed_total = homework_totals.one()
    homework_completion_rate_percent = (
        round((completed_total or 0) / assigned_total * 100) if assigned_total else 0
    )

    breakdown = []
    for school in schools:
        teacher_count, student_count = await _counts_for_school(session, school.id)
        breakdown.append(
            SchoolBreakdownOut(
                school_id=str(school.id), name=school.name, active_teachers=teacher_count,
                active_students=student_count,
                mastery_avg_percent=await _mastery_avg_for_school(session, school.id),
            )
        )

    # Same real, computed-at-read-time query as
    # src/services/school_analytics.py's per-school trend, just without
    # the school_id filter - every school's Tests pooled into one
    # platform-wide weekly average. No snapshot table, no fake data.
    week_expr = func.date_trunc("week", VoiceTest.created_at)
    trend_rows = await session.execute(
        select(week_expr, func.avg(StudentTestResult.mastery_percent), func.count(StudentTestResult.id))
        .select_from(StudentTestResult)
        .join(VoiceTest, VoiceTest.id == StudentTestResult.test_id)
        .group_by(week_expr)
        .order_by(week_expr)
    )
    mastery_trend = [
        MasteryTrendPointOut(
            period_label=week_start.strftime("%b %-d"),
            mastery_avg_percent=round(avg_mastery) if avg_mastery is not None else 0,
            test_count=count,
        )
        for week_start, avg_mastery, count in trend_rows.all()[-8:]
    ]

    return PlatformAnalyticsOut(
        total_schools=len(schools),
        active_schools=len(active_schools),
        total_teachers=total_teachers.scalar_one(),
        total_students=total_students.scalar_one(),
        tests_this_month=tests_this_month,
        homework_completion_rate_percent=homework_completion_rate_percent,
        school_breakdown=breakdown,
        mastery_trend=mastery_trend,
    )


@router.get("/schools/{school_id}/analytics", summary="One school's own analytics (cross-tenant support view)")
async def get_school_analytics_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAnalyticsOut:
    await _get_school(session, school_id)  # 404s if the school doesn't exist
    return await compute_school_analytics(session, school_id)


@router.get("/school-admins")
async def list_school_admins(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolAdminOut]:
    result = await session.execute(select(User).where(User.role == Role.SCHOOL_ADMIN))
    items = [
        SchoolAdminOut(
            id=str(u.id), school_id=str(u.school_id) if u.school_id else "",
            display_name=u.display_name, email=u.email, status=u.status,
        )
        for u in result.scalars().all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/school-admins/invite", status_code=201)
async def invite_school_admin(
    body: InviteSchoolAdminIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> InviteSchoolAdminOut:
    await _get_school(session, body.school_id)

    provisioned = create_firebase_user(email=body.email, display_name=body.display_name, role=Role.SCHOOL_ADMIN)

    admin_user = User(
        firebase_uid=provisioned.firebase_uid,
        email=body.email,
        display_name=body.display_name,
        role=Role.SCHOOL_ADMIN,
        school_id=body.school_id,
        status=UserStatus.ACTIVE,
        phone_number=body.phone_number,
        must_change_password=True,
    )
    session.add(admin_user)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="school_admin.invited", target_type="user", target_id=str(admin_user.id)
    )
    await session.commit()
    await session.refresh(admin_user)
    return InviteSchoolAdminOut(
        id=str(admin_user.id), school_id=str(admin_user.school_id), display_name=admin_user.display_name,
        email=admin_user.email, status=admin_user.status, temp_password=provisioned.temp_password,
    )


@router.patch("/school-admins/{admin_id}/status")
async def update_school_admin_status(
    admin_id: str,
    body: UpdateSchoolAdminStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminOut:
    result = await session.execute(
        select(User).where(User.id == admin_id, User.role == Role.SCHOOL_ADMIN)
    )
    admin_user = result.scalar_one_or_none()
    if admin_user is None:
        raise NotFoundError(f'No School Admin with id "{admin_id}".')
    admin_user.status = body.status
    await audit_repository.record(
        session, actor_id=actor.id, action="school_admin.status.updated", target_type="user", target_id=admin_id
    )
    await session.commit()
    return SchoolAdminOut(
        id=str(admin_user.id), school_id=str(admin_user.school_id), display_name=admin_user.display_name,
        email=admin_user.email, status=admin_user.status,
    )


async def _get_school_admin(session: AsyncSession, admin_id: str) -> User:
    result = await session.execute(select(User).where(User.id == admin_id, User.role == Role.SCHOOL_ADMIN))
    admin_user = result.scalar_one_or_none()
    if admin_user is None:
        raise NotFoundError(f'No School Admin with id "{admin_id}".')
    return admin_user


@router.patch("/school-admins/{admin_id}", summary="Edit a School Admin's profile")
async def update_school_admin(
    admin_id: str,
    body: UpdateSchoolAdminIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminOut:
    admin_user = await _get_school_admin(session, admin_id)
    if body.display_name is not None:
        admin_user.display_name = body.display_name
    if body.phone_number is not None:
        admin_user.phone_number = body.phone_number
    await audit_repository.record(
        session, actor_id=actor.id, action="school_admin.updated", target_type="user", target_id=admin_id
    )
    await session.commit()
    return SchoolAdminOut(
        id=str(admin_user.id), school_id=str(admin_user.school_id), display_name=admin_user.display_name,
        email=admin_user.email, status=admin_user.status,
    )


@router.post("/school-admins/{admin_id}/reset-password", summary="Issue a new temp password")
async def reset_school_admin_password(
    admin_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ResetPasswordOut:
    admin_user = await _get_school_admin(session, admin_id)
    if admin_user.firebase_uid is None:
        raise ConflictError("This School Admin has no Firebase account to reset.")
    temp_password = reset_password(firebase_uid=admin_user.firebase_uid, role=Role.SCHOOL_ADMIN)
    admin_user.must_change_password = True
    await audit_repository.record(
        session, actor_id=actor.id, action="school_admin.password_reset", target_type="user", target_id=admin_id
    )
    await session.commit()
    return ResetPasswordOut(temp_password=temp_password)


@router.get("/schools/{school_id}/teachers", summary="List a school's teachers (cross-tenant)")
async def list_teachers_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolTeacherOut]:
    await _get_school(session, school_id)
    result = await session.execute(
        select(User).where(User.school_id == school_id, User.role == Role.TEACHER)
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


@router.post("/schools/{school_id}/teachers/invite", status_code=201, summary="Invite a teacher (cross-tenant)")
async def invite_teacher_as_admin(
    school_id: str,
    body: InviteTeacherIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> InviteTeacherOut:
    await _get_school(session, school_id)
    plan = await get_active_plan(session, school_id)
    if plan is not None:
        current = await count_teachers(session, school_id)
        if current >= plan.teacher_limit:
            raise ConflictError(
                f"This school's plan allows up to {plan.teacher_limit} teachers - it already has {current}."
            )

    provisioned = create_firebase_user(email=body.email, display_name=body.display_name, role=Role.TEACHER)

    teacher = User(
        firebase_uid=provisioned.firebase_uid,
        email=body.email, display_name=body.display_name, role=Role.TEACHER,
        school_id=school_id, status=UserStatus.ACTIVE,
        phone_number=body.phone_number, employee_id=body.employee_id,
        must_change_password=True,
    )
    session.add(teacher)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.invited", target_type="user", target_id=str(teacher.id),
        detail=f"{teacher.display_name} ({teacher.email})",
    )
    await session.commit()
    await session.refresh(teacher)
    return InviteTeacherOut(id=str(teacher.id), status=teacher.status, temp_password=provisioned.temp_password)


@router.post(
    "/schools/{school_id}/teachers/bulk-invite",
    summary="Bulk-invite teachers from a .csv or .xlsx file (cross-tenant)",
)
async def bulk_invite_teachers_as_admin(
    school_id: str,
    file: UploadFile = File(...),
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BulkImportResultOut:
    """Same columns/contract as school_admin.py's bulk_invite_teachers
    (displayName, email, phoneNumber, employeeId), enforced against the
    target school_id's own plan rather than the caller's (Super Admin has
    no school of their own).
    """
    await _get_school(session, school_id)
    content = await file.read()
    rows = parse_rows(file.filename or "upload", content)

    plan = await get_active_plan(session, school_id)
    if plan is not None:
        current = await count_teachers(session, school_id)
        if current + len(rows) > plan.teacher_limit:
            raise ConflictError(
                f"This school's plan allows up to {plan.teacher_limit} teachers - it has {current} and this "
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
                role=Role.TEACHER, school_id=school_id, status=UserStatus.ACTIVE,
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
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.bulk_invited", target_type="school",
        target_id=school_id,
        detail=f"{created_count} invited, {error_count} failed",
    )
    await session.commit()
    return BulkImportResultOut(
        results=[BulkRowResultOut(**r.__dict__) for r in results],
        created_count=created_count,
        error_count=error_count,
    )


async def _get_teacher(session: AsyncSession, teacher_id: str) -> User:
    """No school_id filter, unlike school_admin.py's _get_teacher_in_school -
    a teacher_id is globally unique, so Super Admin can act on any teacher
    at any school.
    """
    result = await session.execute(select(User).where(User.id == teacher_id, User.role == Role.TEACHER))
    teacher = result.scalar_one_or_none()
    if teacher is None:
        raise NotFoundError(f'No teacher with id "{teacher_id}".')
    return teacher


@router.patch("/teachers/{teacher_id}", summary="Edit a teacher's profile (cross-tenant)")
async def update_teacher_as_admin(
    teacher_id: str,
    body: UpdateTeacherIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolTeacherOut:
    teacher = await _get_teacher(session, teacher_id)
    if body.display_name is not None:
        teacher.display_name = body.display_name
    if body.phone_number is not None:
        teacher.phone_number = body.phone_number
    if body.employee_id is not None:
        teacher.employee_id = body.employee_id
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.updated", target_type="user", target_id=teacher_id
    )
    await session.commit()
    assignments = await session.execute(
        select(TeacherClassAssignment.class_id).where(TeacherClassAssignment.teacher_id == teacher.id)
    )
    return _teacher_out(teacher, list({str(row[0]) for row in assignments.all()}))


@router.patch("/teachers/{teacher_id}/status", summary="Activate/deactivate a teacher (cross-tenant)")
async def update_teacher_status_as_admin(
    teacher_id: str,
    body: UpdateTeacherStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolTeacherOut:
    teacher = await _get_teacher(session, teacher_id)
    teacher.status = body.status
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.status.updated", target_type="user", target_id=teacher_id,
        detail=f"{teacher.display_name} -> {body.status.value}",
    )
    await session.commit()
    assignments = await session.execute(
        select(TeacherClassAssignment.class_id).where(TeacherClassAssignment.teacher_id == teacher.id)
    )
    return _teacher_out(teacher, list({str(row[0]) for row in assignments.all()}))


@router.post("/teachers/{teacher_id}/reset-password", summary="Issue a new temp password (cross-tenant)")
async def reset_teacher_password_as_admin(
    teacher_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ResetPasswordOut:
    """The Super Admin equivalent of school_admin.py's
    reset_teacher_password - the no-email recovery path for a locked-out
    teacher at any school, not just the caller's own.
    """
    teacher = await _get_teacher(session, teacher_id)
    if teacher.firebase_uid is None:
        raise ConflictError("This teacher has no Firebase account to reset.")
    temp_password = reset_password(firebase_uid=teacher.firebase_uid, role=Role.TEACHER)
    teacher.must_change_password = True
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.password_reset", target_type="user", target_id=teacher_id
    )
    await session.commit()
    return ResetPasswordOut(temp_password=temp_password)


@router.post(
    "/teachers/{teacher_id}/send-credentials-email",
    summary="Email a temp password the admin has reviewed (cross-tenant)",
)
async def send_teacher_credentials_email_as_admin(
    teacher_id: str,
    body: SendCredentialsEmailIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SendCredentialsEmailOut:
    teacher = await _get_teacher(session, teacher_id)
    send_email(to_email=teacher.email, subject=body.subject, message=body.message)
    await audit_repository.record(
        session, actor_id=actor.id, action="teacher.credentials_emailed", target_type="user", target_id=teacher_id
    )
    await session.commit()
    return SendCredentialsEmailOut(sent=True)


@router.delete("/teachers/{teacher_id}", summary="Permanently delete a teacher (cross-tenant)")
async def delete_teacher_as_admin(
    teacher_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Same idempotent/blocked-up-front shape as school_admin.py's
    delete_teacher, just without the school_id filter.
    """
    result = await session.execute(select(User).where(User.id == teacher_id, User.role == Role.TEACHER))
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


@router.get("/schools/{school_id}/teachers/export", summary="Download a school's teacher roster as CSV (cross-tenant)")
async def export_teachers_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _get_school(session, school_id)
    result = await session.execute(
        select(User).where(User.school_id == school_id, User.role == Role.TEACHER)
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


@router.get("/schools/{school_id}/students", summary="List a school's students (cross-tenant)")
async def list_students_as_admin(
    school_id: str,
    class_id: str | None = Query(default=None, alias="classId"),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolStudentOut]:
    await _get_school(session, school_id)
    query = (
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(SchoolClass, SchoolClass.id == StudentProfile.class_id)
        .where(SchoolClass.school_id == school_id)
    )
    if class_id:
        query = query.where(StudentProfile.class_id == class_id)
    result = await session.execute(query)
    items = [_student_out(u, sp) for u, sp in result.all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/schools/{school_id}/students", status_code=201, summary="Create a student (cross-tenant)")
async def create_student_as_admin(
    school_id: str,
    body: CreateStudentIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> CreateStudentOut:
    await _get_school(session, school_id)
    await get_class_in_school(session, body.class_id, school_id)  # the class must be this school's
    plan = await get_active_plan(session, school_id)
    if plan is not None:
        current = await count_students(session, school_id)
        if current >= plan.student_limit:
            raise ConflictError(
                f"This school's plan allows up to {plan.student_limit} students - it already has {current}."
            )

    provisioned = create_firebase_user(email=body.email, display_name=body.display_name, role=Role.STUDENT)

    student_user = User(
        firebase_uid=provisioned.firebase_uid, email=body.email,
        display_name=body.display_name, role=Role.STUDENT, school_id=school_id,
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
        session, actor_id=actor.id, action="student.created", target_type="user", target_id=str(student_user.id),
        detail=f"{student_user.display_name} ({student_user.email})",
    )
    await session.commit()
    return CreateStudentOut(
        id=str(student_user.id), display_name=student_user.display_name, email=student_user.email,
        class_id=body.class_id, roll_number=body.roll_number, status=student_user.status,
        temp_password=provisioned.temp_password,
    )


@router.post(
    "/schools/{school_id}/students/bulk-create",
    summary="Bulk-create students from a .csv or .xlsx file (cross-tenant)",
)
async def bulk_create_students_as_admin(
    school_id: str,
    file: UploadFile = File(...),
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> BulkImportResultOut:
    """Same columns/contract as school_admin.py's bulk_create_students
    (displayName, email, classGrade, classSection, rollNumber, guardianName,
    guardianPhone, dateOfBirth), enforced against the target school_id's own
    plan rather than the caller's (Super Admin has no school of their own).
    """
    await _get_school(session, school_id)
    content = await file.read()
    rows = parse_rows(file.filename or "upload", content)

    plan = await get_active_plan(session, school_id)
    if plan is not None:
        current = await count_students(session, school_id)
        if current + len(rows) > plan.student_limit:
            raise ConflictError(
                f"This school's plan allows up to {plan.student_limit} students - it has {current} and this "
                f"file would add {len(rows)}, {current + len(rows) - plan.student_limit} over the limit."
            )

    classes_result = await session.execute(select(SchoolClass).where(SchoolClass.school_id == school_id))
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
                BulkRowResult(row=index, status="error", email=email, error=f"No class {grade_raw}-{section} in this school.")
            )
            continue

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
                role=Role.STUDENT, school_id=school_id, status=UserStatus.ACTIVE,
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
        session, actor_id=actor.id, action="student.bulk_created", target_type="school",
        target_id=school_id,
        detail=f"{created_count} created, {error_count} failed",
    )
    await session.commit()
    return BulkImportResultOut(
        results=[BulkRowResultOut(**r.__dict__) for r in results],
        created_count=created_count,
        error_count=error_count,
    )


async def _get_student(session: AsyncSession, student_id: str) -> tuple[User, StudentProfile]:
    """No school_id filter, unlike school_admin.py's own lookups - a
    student_id is globally unique, so Super Admin can act on any student at
    any school.
    """
    result = await session.execute(
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .where(User.id == student_id)
    )
    row = result.first()
    if row is None:
        raise NotFoundError(f'No student with id "{student_id}".')
    return row


@router.patch("/students/{student_id}", summary="Reassign class / change status (cross-tenant)")
async def update_student_as_admin(
    student_id: str,
    body: UpdateStudentIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolStudentOut:
    student_user, profile = await _get_student(session, student_id)
    if body.class_id is not None:
        await get_class_in_school(session, body.class_id, student_user.school_id)  # stay within the school
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
        session, actor_id=actor.id, action="student.updated", target_type="user", target_id=student_id
    )
    await session.commit()
    return _student_out(student_user, profile)


@router.post("/students/{student_id}/reset-password", summary="Issue a new temp password (cross-tenant)")
async def reset_student_password_as_admin(
    student_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ResetPasswordOut:
    """The Super Admin equivalent of school_admin.py's
    reset_student_password - the no-email recovery path for a locked-out
    student at any school, not just the caller's own.
    """
    student_user, _profile = await _get_student(session, student_id)
    if student_user.firebase_uid is None:
        raise ConflictError("This student has no Firebase account to reset.")
    temp_password = reset_password(firebase_uid=student_user.firebase_uid, role=Role.STUDENT)
    student_user.must_change_password = True
    await audit_repository.record(
        session, actor_id=actor.id, action="student.password_reset", target_type="user", target_id=student_id
    )
    await session.commit()
    return ResetPasswordOut(temp_password=temp_password)


@router.get("/schools/{school_id}/students/export", summary="Download a school's student roster as CSV (cross-tenant)")
async def export_students_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _get_school(session, school_id)
    result = await session.execute(
        select(User, StudentProfile, SchoolClass)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(SchoolClass, SchoolClass.id == StudentProfile.class_id)
        .where(SchoolClass.school_id == school_id)
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


@router.post(
    "/students/{student_id}/send-credentials-email",
    summary="Email a temp password the admin has reviewed (cross-tenant)",
)
async def send_student_credentials_email_as_admin(
    student_id: str,
    body: SendCredentialsEmailIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SendCredentialsEmailOut:
    student_user, _profile = await _get_student(session, student_id)
    send_email(to_email=student_user.email, subject=body.subject, message=body.message)
    await audit_repository.record(
        session, actor_id=actor.id, action="student.credentials_emailed", target_type="user", target_id=student_id
    )
    await session.commit()
    return SendCredentialsEmailOut(sent=True)


@router.delete("/students/{student_id}", summary="Permanently delete a student (cross-tenant)")
async def delete_student_as_admin(
    student_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Same idempotent/blocked-up-front shape as school_admin.py's
    delete_student, just without the school_id filter.
    """
    result = await session.execute(
        select(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .where(User.id == student_id)
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


async def _get_class(session: AsyncSession, class_id: str) -> SchoolClass:
    """No school_id filter, unlike school_admin.py's own lookups - a
    class_id is globally unique, so Super Admin can act on any class at
    any school.
    """
    result = await session.execute(select(SchoolClass).where(SchoolClass.id == class_id))
    school_class = result.scalar_one_or_none()
    if school_class is None:
        raise NotFoundError(f'No class with id "{class_id}".')
    return school_class


async def _class_out(session: AsyncSession, school_class: SchoolClass) -> SchoolAdminClassOut:
    assignments = await session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.class_id == school_class.id)
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


@router.get("/schools/{school_id}/classes", summary="List a school's classes (cross-tenant)")
async def list_classes_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolAdminClassOut]:
    await _get_school(session, school_id)
    result = await session.execute(select(SchoolClass).where(SchoolClass.school_id == school_id))
    classes = result.scalars().all()
    items = [await _class_out(session, c) for c in classes]
    return ListEnvelope(items=items, total=len(items))


@router.post("/schools/{school_id}/classes", status_code=201, summary="Create a class (cross-tenant)")
async def create_class_as_admin(
    school_id: str,
    body: CreateClassIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    await _get_school(session, school_id)
    grade_row = await get_or_create_grade(session, school_id, body.grade)
    school_class = SchoolClass(
        school_id=school_id, grade=body.grade, section=body.section, grade_id=grade_row.id,
    )
    session.add(school_class)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="class.created", target_type="class", target_id=str(school_class.id),
        detail=f"Class {school_class.grade} · {school_class.section}",
    )
    await session.commit()
    await session.refresh(school_class)
    return await _class_out(session, school_class)


@router.patch("/classes/{class_id}", summary="Edit a class's grade/section (cross-tenant)")
async def update_class_as_admin(
    class_id: str,
    body: UpdateClassIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    school_class = await _get_class(session, class_id)
    old_label = f"Class {school_class.grade} · {school_class.section}"
    await ensure_class_grade_change_allowed(session, school_class, body.grade)
    if body.grade != school_class.grade:
        grade_row = await get_or_create_grade(session, school_class.school_id, body.grade)
        school_class.grade_id = grade_row.id
    school_class.grade = body.grade
    school_class.section = body.section
    await audit_repository.record(
        session, actor_id=actor.id, action="class.updated", target_type="class", target_id=class_id,
        detail=f"{old_label} -> Class {body.grade} · {body.section}",
    )
    await session.commit()
    return await _class_out(session, school_class)


@router.patch("/classes/{class_id}/status", summary="Activate/deactivate a class (cross-tenant)")
async def update_class_status_as_admin(
    class_id: str,
    body: UpdateClassStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    school_class = await _get_class(session, class_id)
    school_class.status = body.status
    await audit_repository.record(
        session, actor_id=actor.id, action="class.status.updated", target_type="class", target_id=class_id,
        detail=f"Class {school_class.grade} · {school_class.section} -> {body.status.value}",
    )
    await session.commit()
    return await _class_out(session, school_class)


@router.delete("/classes/{class_id}", summary="Permanently delete a class (cross-tenant)")
async def delete_class_as_admin(
    class_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Same idempotent/blocked-up-front shape as school_admin.py's
    delete_school_class, just without the school_id filter.
    """
    result = await session.execute(select(SchoolClass).where(SchoolClass.id == class_id))
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
        session, actor_id=actor.id, action="class.deleted", target_type="class", target_id=class_id,
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


async def _get_grade(session: AsyncSession, grade_id: str) -> SchoolGrade:
    """No school_id filter, same reasoning as _get_class - a grade_id is
    globally unique, Super Admin can act on any school's Class."""
    result = await session.execute(select(SchoolGrade).where(SchoolGrade.id == grade_id))
    school_grade = result.scalar_one_or_none()
    if school_grade is None:
        raise NotFoundError(f'No Class with id "{grade_id}".')
    return school_grade


@router.get("/schools/{school_id}/grades", summary="List a school's Classes (cross-tenant)")
async def list_grades_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolGradeOut]:
    await _get_school(session, school_id)
    result = await session.execute(select(SchoolGrade).where(SchoolGrade.school_id == school_id))
    items = [await _grade_out(session, g) for g in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/schools/{school_id}/grades", status_code=201, summary="Create a Class (cross-tenant)")
async def create_grade_as_admin(
    school_id: str,
    body: CreateGradeIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolGradeOut:
    await _get_school(session, school_id)
    existing = await session.execute(
        select(SchoolGrade.id).where(SchoolGrade.school_id == school_id, SchoolGrade.grade == body.grade)
    )
    if existing.first() is not None:
        raise ConflictError(f"Class {body.grade} already exists at this school.")
    school_grade = SchoolGrade(school_id=school_id, grade=body.grade)
    session.add(school_grade)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="grade.created", target_type="grade", target_id=str(school_grade.id),
        detail=f"Class {school_grade.grade}",
    )
    await session.commit()
    return await _grade_out(session, school_grade)


@router.patch("/grades/{grade_id}/status", summary="Activate/deactivate a Class (cross-tenant)")
async def update_grade_status_as_admin(
    grade_id: str,
    body: UpdateGradeStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolGradeOut:
    school_grade = await _get_grade(session, grade_id)
    school_grade.status = body.status
    await audit_repository.record(
        session, actor_id=actor.id, action="grade.status.updated", target_type="grade", target_id=grade_id,
        detail=f"Class {school_grade.grade} -> {body.status.value}",
    )
    await session.commit()
    return await _grade_out(session, school_grade)


@router.delete("/grades/{grade_id}", summary="Permanently delete a Class (cross-tenant)")
async def delete_grade_as_admin(
    grade_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(select(SchoolGrade).where(SchoolGrade.id == grade_id))
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
        session, actor_id=actor.id, action="grade.deleted", target_type="grade", target_id=grade_id,
        detail=f"Class {school_grade.grade}",
    )
    await session.commit()
    return {"deleted": True}


async def _grade_subjects_out(session: AsyncSession, grade_id: str, grade: int) -> GradeSubjectsOut:
    subjects = await picker_subjects_for_grade(session, grade_id, grade)
    grades_map = await grades_by_subject(session, [s.id for s in subjects])
    enabled_result = await session.execute(
        select(GradeSubject.subject_id).where(GradeSubject.grade_id == grade_id, GradeSubject.enabled.is_(True))
    )
    enabled_ids = {row[0] for row in enabled_result.all()}
    return GradeSubjectsOut(
        grade_id=grade_id,
        subjects=[
            SubjectToggle(
                id=str(s.id), name=s.name, grade=s.grade, enabled=s.id in enabled_ids, grades=grades_map.get(str(s.id), [])
            )
            for s in subjects
        ],
    )


@router.get("/grades/{grade_id}/subjects", summary="View which subjects a Class teaches (cross-tenant)")
async def get_grade_subjects_as_admin(
    grade_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> GradeSubjectsOut:
    school_grade = await _get_grade(session, grade_id)
    return await _grade_subjects_out(session, grade_id, school_grade.grade)


@router.put("/grades/{grade_id}/subjects", summary="Set which subjects a Class teaches (cross-tenant)")
async def update_grade_subjects_as_admin(
    grade_id: str,
    body: UpdateGradeSubjectsIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> GradeSubjectsOut:
    school_grade = await _get_grade(session, grade_id)
    existing = await session.execute(select(GradeSubject).where(GradeSubject.grade_id == grade_id))
    by_subject = {str(row.subject_id): row for row in existing.scalars().all()}

    enabled_ids = set(body.subject_ids)
    new_grants = enabled_ids - set(by_subject.keys())
    await validate_new_grants_for_grade(session, new_grants, school_grade.grade)
    await ensure_subjects_enabled_for_school(session, new_grants, school_grade.school_id)

    for subject_id, row in by_subject.items():
        row.enabled = subject_id in enabled_ids
    for subject_id in new_grants:
        session.add(GradeSubject(grade_id=grade_id, subject_id=subject_id, enabled=True))
    await audit_repository.record(
        session, actor_id=actor.id, action="grade.subjects.updated", target_type="grade", target_id=grade_id,
    )
    await session.commit()
    return await _grade_subjects_out(session, grade_id, school_grade.grade)


@router.put("/classes/{class_id}/assignments", summary="Assign teacher + subject (cross-tenant)")
async def update_class_assignments_as_admin(
    class_id: str,
    body: UpdateClassAssignmentsIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolAdminClassOut:
    """Full replace of this class's teacher/subject pairings, same pattern
    as school_admin.py's update_class_assignments, just without the
    school_id filter.
    """
    school_class = await _get_class(session, class_id)

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
        session, actor_id=actor.id, action="class.assignments.updated", target_type="class", target_id=class_id,
        detail=f"{len(body.assignments)} assignment{'s' if len(body.assignments) != 1 else ''}",
    )
    await session.commit()
    return await _class_out(session, school_class)


@router.get("/schools/{school_id}/curriculum", summary="View a school's enabled subjects (cross-tenant)")
async def get_curriculum_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolCurriculumOut:
    await _get_school(session, school_id)
    subjects = await picker_subjects_for_school(session, school_id)
    grades_map = await grades_by_subject(session, [s.id for s in subjects])
    enabled_result = await session.execute(
        select(SchoolCurriculum.subject_id).where(
            SchoolCurriculum.school_id == school_id, SchoolCurriculum.enabled.is_(True)
        )
    )
    enabled_ids = {row[0] for row in enabled_result.all()}
    return SchoolCurriculumOut(
        board="",
        subjects=[
            SubjectToggle(
                id=str(s.id), name=s.name, grade=s.grade, enabled=s.id in enabled_ids, grades=grades_map.get(str(s.id), [])
            )
            for s in subjects
        ],
    )


@router.put("/schools/{school_id}/curriculum", summary="Enable/disable subjects for a school (cross-tenant)")
async def update_curriculum_as_admin(
    school_id: str,
    body: UpdateSchoolCurriculumIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SchoolCurriculumOut:
    await _get_school(session, school_id)
    existing = await session.execute(
        select(SchoolCurriculum).where(SchoolCurriculum.school_id == school_id)
    )
    # Keyed by str(subject_id) - see school_admin.py's update_school_curriculum
    # for why: body.subject_ids is list[str] off the wire, row.subject_id
    # is a UUID object, and comparing them directly would silently never
    # match, re-inserting every already-enabled subject and hitting
    # SchoolCurriculum's (school_id, subject_id) unique constraint.
    by_subject = {str(row.subject_id): row for row in existing.scalars().all()}

    enabled_ids = set(body.subject_ids)
    new_grants = enabled_ids - set(by_subject.keys())
    await validate_new_grants_have_any_grade(session, new_grants)

    for subject_id, row in by_subject.items():
        row.enabled = subject_id in enabled_ids
    for subject_id in new_grants:
        session.add(SchoolCurriculum(school_id=school_id, subject_id=subject_id, enabled=True))
    await audit_repository.record(
        session, actor_id=actor.id, action="curriculum.updated", target_type="school", target_id=school_id
    )
    await session.commit()

    return await get_curriculum_as_admin(school_id, _=actor, session=session)


@router.post(
    "/schools/{school_id}/curriculum/subjects",
    status_code=201,
    summary="Add a subject not yet in the catalog, enabled for a school (cross-tenant)",
)
async def create_subject_as_admin(
    school_id: str,
    body: CreateSchoolSubjectIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SubjectToggle:
    """Reuses an existing Subject by name (case-insensitive) or creates one,
    then enables it for an explicit school_id. body.grades is required
    (CreateSchoolSubjectIn) so the enable-for-this-school step below always
    has a subject that's actually eligible - unioned onto an existing
    subject's grades rather than replacing them, since two schools asking
    for "Sanskrit" at different grades should both stick.
    """
    await _get_school(session, school_id)
    name = body.name.strip()
    if not name:
        raise ConflictError("Subject name can't be empty.")

    # subjects are per grade: reuse this name's subject for *this* grade only
    existing = await session.execute(
        select(Subject).where(func.lower(Subject.name) == name.lower(), Subject.grade == body.grade))
    subject = existing.scalar_one_or_none()
    if subject is None:
        subject = Subject(name=name, grade=body.grade)
        session.add(subject)
        await session.flush()
        await audit_repository.record(
            session, actor_id=actor.id, action="subject.created", target_type="subject", target_id=str(subject.id),
            detail=subject.name,
        )
    await add_subject_grades(session, subject.id, {body.grade})

    curriculum_row = await session.execute(
        select(SchoolCurriculum).where(
            SchoolCurriculum.school_id == school_id, SchoolCurriculum.subject_id == subject.id
        )
    )
    row = curriculum_row.scalar_one_or_none()
    if row is None:
        session.add(SchoolCurriculum(school_id=school_id, subject_id=subject.id, enabled=True))
    else:
        row.enabled = True
    await audit_repository.record(
        session, actor_id=actor.id, action="curriculum.subject_added", target_type="subject", target_id=str(subject.id),
        detail=subject.name,
    )
    await session.commit()
    grades_map = await grades_by_subject(session, [subject.id])
    return SubjectToggle(
        id=str(subject.id), name=subject.name, grade=subject.grade, enabled=True, grades=grades_map.get(str(subject.id), [])
    )


@router.get("/schools/{school_id}/datasets", summary="A school's datasets: assigned catalog + its own (cross-tenant)")
async def get_datasets_as_admin(
    school_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolDatasetOut]:
    """Every catalog dataset with whether it's assigned to this school
    (`enabled`), plus the school's own datasets (`owned`, view only here)."""
    await _get_school(session, school_id)
    listing = await school_dataset_listing(session, school_id, include_unassigned=True)
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


@router.put("/schools/{school_id}/datasets", summary="Assign catalog datasets to a school (cross-tenant)")
async def update_datasets_as_admin(
    school_id: str,
    body: UpdateSchoolDatasetsIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolDatasetOut]:
    """`datasetIds` = the full set of catalog datasets the school should have.
    Only a Super Admin assigns datasets; a school's own datasets aren't part
    of this (and can't be assigned to another school)."""
    await _get_school(session, school_id)
    await assign_catalog_datasets(session, school_id, body.dataset_ids)
    await audit_repository.record(
        session, actor_id=actor.id, action="datasets.updated", target_type="school", target_id=school_id
    )
    await session.commit()

    return await get_datasets_as_admin(school_id, _=actor, session=session)


@router.get("/audit-log")
async def list_audit_log(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[AuditLogEntryOut]:
    total = (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    result = await session.execute(
        select(AuditLog, User.display_name).outerjoin(User, User.id == AuditLog.actor_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    )
    items = [
        AuditLogEntryOut(
            id=str(a.id), actor_id=str(a.actor_id), actor_name=actor_name, action=a.action,
            target_type=a.target_type, target_id=a.target_id, detail=a.detail, created_at=a.created_at,
        )
        for a, actor_name in result.all()
    ]
    return ListEnvelope(items=items, total=total)


@router.get("/upgrade-requests", summary="Super Admin's upgrade-request inbox")
async def list_upgrade_requests(
    status: UpgradeRequestStatus | None = Query(default=None),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[UpgradeRequestSummaryOut]:
    """Closes the loop School Admin's "Request an upgrade" CTA otherwise
    dead-ends into - see UpgradeRequest's model docstring. Resolves school
    and requester names server-side rather than leaving the client to
    join raw ids, unlike the flat audit log above.
    """
    requester = aliased(User)
    query = (
        select(UpgradeRequest, School.name, requester.display_name, requester.email)
        .join(School, School.id == UpgradeRequest.school_id)
        .join(requester, requester.id == UpgradeRequest.requested_by)
        .order_by(UpgradeRequest.created_at.desc())
    )
    if status is not None:
        query = query.where(UpgradeRequest.status == status)
    result = await session.execute(query)
    items = [
        UpgradeRequestSummaryOut(
            id=str(req.id), school_id=str(req.school_id), school_name=school_name,
            requested_by_name=requester_name, requested_by_email=requester_email,
            message=req.message, status=req.status, created_at=req.created_at, resolved_at=req.resolved_at,
        )
        for req, school_name, requester_name, requester_email in result.all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.patch("/upgrade-requests/{request_id}/status", summary="Mark an upgrade request contacted/resolved")
async def update_upgrade_request_status(
    request_id: str,
    body: UpdateUpgradeRequestStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> UpgradeRequestSummaryOut:
    result = await session.execute(select(UpgradeRequest).where(UpgradeRequest.id == request_id))
    req = result.scalar_one_or_none()
    if req is None:
        raise NotFoundError(f'No upgrade request with id "{request_id}".')
    req.status = body.status
    req.resolved_at = datetime.now(timezone.utc) if body.status != UpgradeRequestStatus.PENDING else None
    req.resolved_by = actor.id if body.status != UpgradeRequestStatus.PENDING else None
    await audit_repository.record(
        session, actor_id=actor.id, action="upgrade_request.status.updated",
        target_type="upgrade_request", target_id=request_id, detail=body.status.value,
    )
    await session.commit()

    school = await session.get(School, req.school_id)
    requester = await session.get(User, req.requested_by)
    return UpgradeRequestSummaryOut(
        id=str(req.id), school_id=str(req.school_id), school_name=school.name if school else "",
        requested_by_name=requester.display_name if requester else "", requested_by_email=requester.email if requester else "",
        message=req.message, status=req.status, created_at=req.created_at, resolved_at=req.resolved_at,
    )


# ---- Support tickets (see SupportTicket's model docstring) -----------------

async def _support_ticket_out(session: AsyncSession, ticket: SupportTicket) -> SupportTicketOut:
    school = await session.get(School, ticket.school_id)
    creator = await session.get(User, ticket.created_by)
    count_result = await session.execute(
        select(func.count()).select_from(SupportTicketComment).where(SupportTicketComment.ticket_id == ticket.id)
    )
    return SupportTicketOut(
        id=str(ticket.id), school_id=str(ticket.school_id), school_name=school.name if school else "",
        created_by=str(ticket.created_by), created_by_name=creator.display_name if creator else "",
        subject=ticket.subject, description=ticket.description, status=ticket.status,
        created_at=ticket.created_at, comment_count=count_result.scalar_one(),
    )


@router.get("/tickets", summary="Super Admin's support-ticket inbox")
async def list_support_tickets(
    status: TicketStatus | None = Query(default=None),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SupportTicketOut]:
    query = select(SupportTicket).order_by(SupportTicket.created_at.desc())
    if status is not None:
        query = query.where(SupportTicket.status == status)
    result = await session.execute(query)
    items = [await _support_ticket_out(session, t) for t in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/tickets/{ticket_id}", summary="One ticket's detail, any school")
async def get_support_ticket(
    ticket_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SupportTicketOut:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError(f'No ticket with id "{ticket_id}".')
    return await _support_ticket_out(session, ticket)


@router.patch("/tickets/{ticket_id}/status", summary="Change a ticket's status")
async def update_support_ticket_status(
    ticket_id: str,
    body: UpdateTicketStatusIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> SupportTicketOut:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError(f'No ticket with id "{ticket_id}".')
    ticket.status = body.status
    await audit_repository.record(
        session, actor_id=actor.id, action="ticket.status.updated",
        target_type="support_ticket", target_id=ticket_id, detail=body.status.value,
    )
    await session.commit()
    return await _support_ticket_out(session, ticket)


@router.get("/tickets/{ticket_id}/comments", summary="A ticket's comment thread, any school")
async def list_support_ticket_comments(
    ticket_id: str,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[TicketCommentOut]:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError(f'No ticket with id "{ticket_id}".')
    result = await session.execute(
        select(SupportTicketComment).where(SupportTicketComment.ticket_id == ticket_id).order_by(
            SupportTicketComment.created_at
        )
    )
    comments = result.scalars().all()
    authors = {a.id: a for a in (
        await session.execute(select(User).where(User.id.in_({c.author_id for c in comments})))
    ).scalars().all()} if comments else {}
    items = [
        TicketCommentOut(
            id=str(c.id), author_id=str(c.author_id),
            author_name=authors[c.author_id].display_name if c.author_id in authors else "",
            body=c.body, created_at=c.created_at,
        )
        for c in comments
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/tickets/{ticket_id}/comments", status_code=201, summary="Reply on a ticket, any school")
async def create_support_ticket_comment(
    ticket_id: str,
    body: CreateTicketCommentIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> TicketCommentOut:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError(f'No ticket with id "{ticket_id}".')
    comment = SupportTicketComment(ticket_id=ticket_id, author_id=actor.id, body=body.body)
    session.add(comment)
    await audit_repository.record(
        session, actor_id=actor.id, action="ticket.comment.created",
        target_type="support_ticket", target_id=ticket_id,
    )
    await session.commit()
    await session.refresh(comment)
    return TicketCommentOut(
        id=str(comment.id), author_id=str(comment.author_id), author_name=actor.display_name,
        body=comment.body, created_at=comment.created_at,
    )


# --- Cross-tenant, read-only school oversight (Super Admin) ---------------
#
# Mirrors src/api/routes/school_oversight.py route-for-route - School Admin
# never writes this content, only observes it, and neither does Super Admin
# here - every route below is a GET, scoped by an explicit school_id path
# param instead of user.school_id. _class_label/_teacher_names_for are a
# deliberate small duplication of school_oversight.py's own (private,
# leading-underscore) helpers rather than an import, matching this file's
# existing _get_school-vs-school_admin precedent.


def _class_label(school_class: SchoolClass) -> str:
    return f"Class {school_class.grade} · {school_class.section}"


async def _teacher_names_for(
    session: AsyncSession, class_subject_pairs: set[tuple]
) -> dict[tuple, str]:
    """Best-effort (class_id, subject_id) -> teacher display name lookup,
    same approach as school_oversight.py's own helper of the same name.
    """
    if not class_subject_pairs:
        return {}
    class_ids = {pair[0] for pair in class_subject_pairs}
    subject_ids = {pair[1] for pair in class_subject_pairs}
    result = await session.execute(
        select(TeacherClassAssignment.class_id, TeacherClassAssignment.subject_id, User.display_name)
        .join(User, User.id == TeacherClassAssignment.teacher_id)
        .where(
            TeacherClassAssignment.class_id.in_(class_ids),
            TeacherClassAssignment.subject_id.in_(subject_ids),
        )
    )
    names: dict[tuple, str] = {}
    for class_id, subject_id, display_name in result.all():
        names.setdefault((class_id, subject_id), display_name)
    return names


def _as_school(school) -> SimpleNamespace:
    """The school-admin oversight queries, run for any school (super admin)."""
    return SimpleNamespace(school_id=school.id)


@router.get("/schools/{school_id}/voice-tests", summary="A school's Speak with Milo sessions (cross-tenant oversight)")
async def list_school_voice_tests_as_admin(
    school_id: str,
    class_id: str | None = Query(default=None, alias="classId"),
    teacher_id: str | None = Query(default=None, alias="teacherId"),
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolVoiceTestOut]:
    school = await _get_school(session, school_id)
    return await school_oversight.list_school_voice_tests(
        class_id=class_id, teacher_id=teacher_id, q=q, limit=limit, offset=offset, user=_as_school(school),
        session=session)


@router.get("/schools/{school_id}/homework", summary="A school's Homework (cross-tenant oversight)")
async def list_school_homework_as_admin(
    school_id: str,
    class_id: str | None = Query(default=None, alias="classId"),
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolHomeworkOut]:
    school = await _get_school(session, school_id)
    return await school_oversight.list_school_homework(
        class_id=class_id, q=q, limit=limit, offset=offset, user=_as_school(school), session=session)


@router.get("/schools/{school_id}/challenges", summary="A school's Maths challenges (cross-tenant oversight)")
async def list_school_challenges_as_admin(
    school_id: str,
    class_id: str | None = Query(default=None, alias="classId"),
    teacher_id: str | None = Query(default=None, alias="teacherId"),
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolChallengeOut]:
    school = await _get_school(session, school_id)
    return await school_oversight.list_school_challenges(
        class_id=class_id, teacher_id=teacher_id, q=q, limit=limit, offset=offset, user=_as_school(school),
        session=session)


@router.get("/schools/{school_id}/question-papers", summary="A school's Question Papers (cross-tenant oversight)")
async def list_school_question_papers_as_admin(
    school_id: str,
    subject_id: str | None = Query(default=None, alias="subjectId"),
    created_by: str | None = Query(default=None, alias="createdBy"),
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolQuestionPaperOut]:
    school = await _get_school(session, school_id)
    return await school_oversight.list_school_question_papers(
        subject_id=subject_id, created_by=created_by, q=q, limit=limit, offset=offset, user=_as_school(school),
        session=session)


@router.get("/schools/{school_id}/leaderboard", summary="A school-wide leaderboard (cross-tenant oversight)")
async def get_school_leaderboard_as_admin(
    school_id: str,
    class_id: str | None = Query(default=None, alias="classId"),
    limit: int = 50,
    offset: int = 0,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolLeaderboardEntryOut]:
    """Same StudentPoints-ranked-across-the-school shape as
    school_oversight.py's get_school_leaderboard, scoped by an explicit
    school_id instead of user.school_id. classId narrows the ranking to a
    single class/section - rank() is computed over the already-filtered
    rows, so a class-scoped request ranks 1..N within that class, not the
    whole school's rank re-sliced.
    """
    await _get_school(session, school_id)
    rank = func.rank().over(order_by=StudentPoints.points.desc())
    base = (
        select(User.id, User.display_name, SchoolClass, StudentPoints.points, rank.label("rank"))
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(SchoolClass, SchoolClass.id == StudentProfile.class_id)
        .join(StudentPoints, StudentPoints.student_id == User.id, isouter=True)
        .where(SchoolClass.school_id == school_id)
    )
    if class_id is not None:
        base = base.where(StudentProfile.class_id == class_id)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(StudentPoints.points.desc().nulls_last()).limit(limit).offset(offset)
        )
    ).all()
    items = [
        SchoolLeaderboardEntryOut(
            student_id=str(student_id),
            display_name=display_name,
            class_label=_class_label(school_class),
            points=points or 0,
            rank=rank_value,
        )
        for student_id, display_name, school_class, points, rank_value in rows
    ]
    return ListEnvelope(items=items, total=total)


@router.get(
    "/schools/{school_id}/audit-log",
    summary="One school's own activity log (cross-tenant oversight - different from GET /admin/audit-log, "
    "which is platform-wide)",
)
async def list_school_audit_log_as_admin(
    school_id: str,
    limit: int = 50,
    offset: int = 0,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolAuditLogEntryOut]:
    """School-scoped counterpart to GET /admin/audit-log (this file's own
    list_audit_log, above) - only entries whose actor belongs to school_id
    (AuditLog has no school_id of its own, so this joins through the
    actor's User row), same as school_oversight.py's list_school_audit_log
    but for an explicit school_id rather than the caller's own school.
    """
    await _get_school(session, school_id)
    base = (
        select(AuditLog, User.display_name)
        .join(User, User.id == AuditLog.actor_id)
        .where(User.school_id == school_id)
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(base.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset))
    ).all()
    items = [
        SchoolAuditLogEntryOut(
            id=str(entry.id), actor_name=actor_name, action=entry.action,
            target_type=entry.target_type, target_id=entry.target_id, detail=entry.detail,
            created_at=entry.created_at,
        )
        for entry, actor_name in rows
    ]
    return ListEnvelope(items=items, total=total)


@router.get(
    "/schools/{school_id}/question-bank",
    summary="Every question generated at this school, grouped by topic (cross-tenant oversight)",
)
async def list_school_question_bank_as_admin(
    school_id: str,
    source: str | None = Query(default=None, description="QUESTION_PAPER, HOMEWORK, or CUSTOM"),
    topic: str | None = Query(default=None, description="Case-insensitive substring match on topic"),
    # Needs an explicit camelCase alias like every other multi-word Query
    # param here - FastAPI doesn't auto-alias query params the way
    # CamelModel does for JSON bodies, so without this the frontend's
    # `?collectionName=` would never bind and the parameter would silently
    # stay at default. See school_oversight.py's list_school_question_bank,
    # which this mirrors.
    collection_name: str | None = Query(default=None, alias="collectionName", description="Exact match, CUSTOM rows only"),
    # A separate boolean flag rather than overloading collection_name=""
    # for "the general bank" - keeps the two cases unambiguous rather
    # than relying on empty-string-vs-omitted query semantics.
    general_bank_only: bool = Query(default=False, alias="generalBankOnly", description="True = only rows with no named set"),
    dataset_id: Annotated[str | None, Query(alias="datasetId", description="One own bank, CUSTOM rows only")] = None,
    limit: int = 50,
    offset: int = 0,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[SchoolQuestionBankEntryOut]:
    await _get_school(session, school_id)
    return await question_bank_entries(
        session, school_id, source=source, topic=topic, collection_name=collection_name,
        general_bank_only=general_bank_only, dataset_id=dataset_id, limit=limit, offset=offset)


# --- Advanced Reporting (Super Admin) ---------------------------------------
# See src/services/reporting.py's DIMENSIONS registry - this router is a
# thin layer over run_report, plus persistence (ReportConfiguration) and
# cross-school sharing (ReportShare).

@router.get("/reports/dimensions", summary="List available report dimensions/metrics")
async def list_report_dimensions(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
) -> list[DimensionSpecOut]:
    return [
        DimensionSpecOut(
            key=spec.key, label=spec.label, scope=spec.scope,
            metrics=[MetricSpecOut(key=m.key, label=m.label) for m in spec.metrics],
        )
        for spec in reporting.DIMENSIONS.values()
    ]


@router.post("/reports/run", summary="Run a report ad-hoc, without saving it")
async def run_report_as_admin(
    body: RunReportIn,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ReportResultOut:
    if body.school_id is not None:
        await _get_school(session, body.school_id)
    rows = await reporting.run_report(
        session, dimension=body.dimension, metrics=body.metrics, filters=body.filters, school_id=body.school_id,
    )
    return ReportResultOut(dimension=body.dimension, metrics=body.metrics, rows=rows)


@router.get("/reports/configs", summary="List my saved reports")
async def list_report_configs_as_admin(
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ReportConfigurationOut]:
    result = await session.execute(
        select(ReportConfiguration)
        .where(ReportConfiguration.owner_id == actor.id)
        .order_by(ReportConfiguration.created_at.desc())
    )
    items = [_report_config_out(c) for c in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.post("/reports/configs", status_code=201, summary="Save a report")
async def create_report_config_as_admin(
    body: CreateReportConfigurationIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ReportConfigurationOut:
    reporting.validate_dimension_and_metrics(body.dimension, body.metrics)
    if body.school_id is not None:
        await _get_school(session, body.school_id)
    config = ReportConfiguration(
        owner_id=actor.id, school_id=body.school_id, name=body.name,
        dimension=body.dimension, metrics=body.metrics, filters=body.filters,
    )
    session.add(config)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="report.created", target_type="report", target_id=str(config.id),
        detail=body.name,
    )
    await session.commit()
    return _report_config_out(config)


async def _get_own_report_config(session: AsyncSession, config_id: str, owner_id) -> ReportConfiguration:
    result = await session.execute(
        select(ReportConfiguration).where(
            ReportConfiguration.id == config_id, ReportConfiguration.owner_id == owner_id
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise NotFoundError(f'No saved report with id "{config_id}".')
    return config


@router.delete("/reports/configs/{config_id}", summary="Delete a saved report")
async def delete_report_config_as_admin(
    config_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    config = await _get_own_report_config(session, config_id, actor.id)
    shares = await session.execute(select(ReportShare).where(ReportShare.report_configuration_id == config.id))
    for share in shares.scalars().all():
        await session.delete(share)
    await session.flush()  # no ORM relationship links these two tables, so explicit ordering is needed
    await session.delete(config)
    await audit_repository.record(
        session, actor_id=actor.id, action="report.deleted", target_type="report", target_id=config_id,
    )
    await session.commit()
    return {"deleted": True}


@router.get("/reports/configs/{config_id}/export", summary="Export a saved report's current results as CSV")
async def export_report_config_as_admin(
    config_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    config = await _get_own_report_config(session, config_id, actor.id)
    rows = await reporting.run_report(
        session, dimension=config.dimension, metrics=config.metrics, filters=config.filters,
        school_id=config.school_id,
    )
    return _rows_to_csv(config.metrics, rows, f"{config.name}.csv")


@router.get("/reports/configs/{config_id}/shares", summary="List who a saved report is shared with")
async def list_report_shares_as_admin(
    config_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ReportShareOut]:
    await _get_own_report_config(session, config_id, actor.id)
    result = await session.execute(
        select(ReportShare, School.name)
        .join(School, School.id == ReportShare.shared_with_school_id)
        .where(ReportShare.report_configuration_id == config_id)
        .order_by(ReportShare.created_at.desc())
    )
    items = [
        ReportShareOut(
            id=str(share.id), report_configuration_id=str(share.report_configuration_id),
            shared_with_school_id=str(share.shared_with_school_id), shared_with_school_name=school_name,
            access_level=share.access_level, expires_at=share.expires_at, created_at=share.created_at,
        )
        for share, school_name in result.all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/reports/configs/{config_id}/shares", status_code=201, summary="Share a saved report with a school")
async def create_report_share_as_admin(
    config_id: str,
    body: CreateReportShareIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ReportShareOut:
    await _get_own_report_config(session, config_id, actor.id)
    school = await _get_school(session, body.shared_with_school_id)
    share = ReportShare(
        report_configuration_id=config_id, shared_by=actor.id, shared_with_school_id=school.id,
        access_level=body.access_level, expires_at=body.expires_at,
    )
    session.add(share)
    await session.flush()
    await audit_repository.record(
        session, actor_id=actor.id, action="report.shared", target_type="report", target_id=config_id,
        detail=f"Shared with {school.name}",
    )
    await session.commit()
    return ReportShareOut(
        id=str(share.id), report_configuration_id=config_id, shared_with_school_id=str(school.id),
        shared_with_school_name=school.name, access_level=share.access_level,
        expires_at=share.expires_at, created_at=share.created_at,
    )


@router.delete("/reports/shares/{share_id}", summary="Revoke a report share")
async def delete_report_share_as_admin(
    share_id: str,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    result = await session.execute(
        select(ReportShare)
        .join(ReportConfiguration, ReportConfiguration.id == ReportShare.report_configuration_id)
        .where(ReportShare.id == share_id, ReportConfiguration.owner_id == actor.id)
    )
    share = result.scalar_one_or_none()
    if share is None:
        return {"deleted": True}
    await session.delete(share)
    await audit_repository.record(
        session, actor_id=actor.id, action="report.share_revoked", target_type="report",
        target_id=str(share.report_configuration_id),
    )
    await session.commit()
    return {"deleted": True}


def _report_config_out(config: ReportConfiguration) -> ReportConfigurationOut:
    return ReportConfigurationOut(
        id=str(config.id), name=config.name, dimension=config.dimension, metrics=config.metrics,
        filters=config.filters, school_id=str(config.school_id) if config.school_id else None,
        created_at=config.created_at,
    )


def _rows_to_csv(metrics: list[str], rows: list[dict], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["label", *metrics])
    for row in rows:
        writer.writerow([row.get("label", ""), *[row.get(m, "") for m in metrics]])
    return Response(
        content=buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# --- Data Explorer (Super Admin, read-only) ---------------------------------
# See src/services/data_explorer.py's ENTITIES registry and its module
# docstring for the security model - never raw SQL, only registered
# tables/columns are reachable, and every query is audit-logged here.

@router.get("/explorer/entities", summary="List queryable entities and their columns")
async def list_explorer_entities(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
) -> list[EntitySpecOut]:
    return [
        EntitySpecOut(
            key=spec.key, label=spec.label,
            columns=[ColumnSpecOut(key=c.key, label=c.label, type=c.type) for c in spec.columns.values()],
        )
        for spec in data_explorer.ENTITIES.values()
    ]


@router.post("/explorer/query", summary="Run a read-only, parameterized query against one entity")
async def run_explorer_query(
    body: ExplorerQueryIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ExplorerQueryOut:
    columns, rows, total = await data_explorer.run_query(
        session, entity=body.entity,
        filters=[f.model_dump() for f in body.filters],
        sort=body.sort.model_dump() if body.sort else None,
        limit=body.limit, offset=body.offset,
    )
    # Audit trail for a platform-wide data-browsing tool - see this
    # module's docstring for why every query, not just writes, is logged
    # here (unlike the rest of this codebase, which only logs mutations).
    await audit_repository.record(
        session, actor_id=actor.id, action="data_explorer.query", target_type="data_explorer", target_id=body.entity,
        detail=f"{len(body.filters)} filter(s), {len(rows)} of {total} row(s) returned",
    )
    await session.commit()
    return ExplorerQueryOut(entity=body.entity, columns=columns, rows=rows, total=total)


@router.post("/explorer/export", summary="Export a query's full matching result set as CSV")
async def export_explorer_query(
    body: ExplorerQueryIn,
    actor: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    # Export always pulls every matching row, ignoring the UI's on-screen
    # limit/offset - a CSV export means "everything that matched," not
    # "whatever page I happened to be viewing."
    export_body = body.model_copy(update={"limit": 200, "offset": 0})
    all_rows: list[dict] = []
    columns: list[str] = []
    while True:
        columns, page_rows, total = await data_explorer.run_query(
            session, entity=export_body.entity,
            filters=[f.model_dump() for f in export_body.filters],
            sort=export_body.sort.model_dump() if export_body.sort else None,
            limit=export_body.limit, offset=export_body.offset,
        )
        all_rows.extend(page_rows)
        if len(all_rows) >= total or not page_rows:
            break
        export_body = export_body.model_copy(update={"offset": export_body.offset + export_body.limit})

    await audit_repository.record(
        session, actor_id=actor.id, action="data_explorer.export", target_type="data_explorer",
        target_id=body.entity, detail=f"{len(body.filters)} filter(s), {len(all_rows)} row(s) exported",
    )
    await session.commit()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in all_rows:
        writer.writerow([row.get(c, "") for c in columns])
    return Response(
        content=buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={body.entity.lower()}.csv"},
    )


# ---- Ingestion visibility (see ImportJob's model docstring) ---------------

@router.get("/import-jobs", summary="Cross-tenant visibility into bulk-import runs")
async def list_import_jobs(
    job_type: ImportJobType | None = Query(default=None, alias="jobType"),
    school_id: str | None = Query(default=None, alias="schoolId"),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ImportJobOut]:
    query = select(ImportJob).order_by(ImportJob.created_at.desc())
    if job_type is not None:
        query = query.where(ImportJob.job_type == job_type)
    if school_id is not None:
        query = query.where(ImportJob.school_id == school_id)
    jobs = (await session.execute(query)).scalars().all()

    schools_by_id = {s.id: s for s in (await session.execute(select(School))).scalars().all()}
    users_by_id = {
        u.id: u for u in (
            await session.execute(select(User).where(User.id.in_({j.initiated_by for j in jobs})))
        ).scalars().all()
    } if jobs else {}

    items = [
        ImportJobOut(
            id=str(job.id), school_id=str(job.school_id),
            school_name=schools_by_id[job.school_id].name if job.school_id in schools_by_id else "",
            initiated_by=str(job.initiated_by),
            initiated_by_name=users_by_id[job.initiated_by].display_name if job.initiated_by in users_by_id else "",
            job_type=job.job_type, filename=job.filename, row_count=job.row_count,
            created_count=job.created_count, error_count=job.error_count, created_at=job.created_at,
        )
        for job in jobs
    ]
    return ListEnvelope(items=items, total=len(items))
