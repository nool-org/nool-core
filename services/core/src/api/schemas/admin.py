from datetime import datetime

from pydantic import Field

from src.domain.models import Feature, SchoolStatus, SubscriptionStatus, UpgradeRequestStatus, UserStatus

from .common import CamelModel
from .school_admin import MasteryTrendPointOut


class SchoolOut(CamelModel):
    id: str
    name: str
    board: str
    city: str
    address: str | None
    pincode: str | None
    contact_email: str
    contact_phone: str | None
    principal_name: str | None
    status: SchoolStatus
    plan_id: str | None
    teacher_count: int
    student_count: int
    created_at: datetime


class CreateSchoolIn(CamelModel):
    name: str
    board: str
    city: str
    contact_email: str
    plan_id: str
    address: str | None = None
    pincode: str | None = None
    contact_phone: str | None = None
    principal_name: str | None = None


class UpdateSchoolStatusIn(CamelModel):
    status: SchoolStatus


class UpdateSchoolIn(CamelModel):
    """General profile edit, separate from the status-only endpoint above -
    partial update, only provided fields change."""

    name: str | None = None
    board: str | None = None
    city: str | None = None
    contact_email: str | None = None
    address: str | None = None
    pincode: str | None = None
    contact_phone: str | None = None
    principal_name: str | None = None


class PlanOut(CamelModel):
    id: str
    name: str
    price_label: str
    teacher_limit: int
    student_limit: int
    test_limit: int | None
    question_paper_limit: int | None
    spoken_days_per_week: int | None = None  # None = every day
    extra_challenges_per_month: int = 0
    # None = the plan bundles every Feature. Set/cleared via the dedicated
    # GET/PUT /admin/plans/{id}/features (not this create/update body) -
    # same "can't tell explicit null from omitted" reason CreateSchoolIn/
    # UpdateSchoolIn never carry default_bloom_distribution either. See
    # Plan.enabled_features' docstring.
    enabled_features: list[Feature] | None
    active: bool


class CreatePlanIn(CamelModel):
    name: str
    price_label: str
    teacher_limit: int
    student_limit: int
    test_limit: int | None = None
    question_paper_limit: int | None = None
    spoken_days_per_week: int | None = Field(default=None, ge=0, le=7)
    extra_challenges_per_month: int = Field(default=0, ge=0, le=10)


class UpdatePlanIn(CamelModel):
    name: str | None = None
    price_label: str | None = None
    teacher_limit: int | None = None
    student_limit: int | None = None
    test_limit: int | None = None
    question_paper_limit: int | None = None
    spoken_days_per_week: int | None = Field(default=None, ge=0, le=7)
    extra_challenges_per_month: int | None = Field(default=None, ge=0, le=10)
    active: bool | None = None


class SeatsOut(CamelModel):
    teachers: int
    students: int


class SubscriptionOut(CamelModel):
    school_id: str
    plan_id: str
    status: SubscriptionStatus
    renews_at: datetime
    seats_used: SeatsOut
    seats_limit: SeatsOut


class CreateSubscriptionIn(CamelModel):
    plan_id: str
    status: SubscriptionStatus = SubscriptionStatus.TRIAL
    renews_at: datetime | None = None


class UpdateSubscriptionIn(CamelModel):
    plan_id: str | None = None
    status: SubscriptionStatus | None = None
    renews_at: datetime | None = None


class SchoolBreakdownOut(CamelModel):
    school_id: str
    name: str
    active_teachers: int
    active_students: int
    mastery_avg_percent: int


class PlatformAnalyticsOut(CamelModel):
    total_schools: int
    active_schools: int
    total_teachers: int
    total_students: int
    tests_this_month: int
    homework_completion_rate_percent: int
    school_breakdown: list[SchoolBreakdownOut]
    # Same real, computed-at-read-time weekly average across every school
    # on the platform - see MasteryTrendPointOut's docstring and
    # src/services/school_analytics.py (this is the school-scoped version
    # of the identical query, just without the school_id filter).
    mastery_trend: list[MasteryTrendPointOut]


class SchoolAdminOut(CamelModel):
    id: str
    school_id: str
    display_name: str
    email: str
    status: UserStatus


class InviteSchoolAdminIn(CamelModel):
    school_id: str
    email: str
    display_name: str
    phone_number: str | None = None


class InviteSchoolAdminOut(CamelModel):
    id: str
    school_id: str
    display_name: str
    email: str
    status: UserStatus
    temp_password: str


class UpdateSchoolAdminStatusIn(CamelModel):
    status: UserStatus


class UpdateSchoolAdminIn(CamelModel):
    display_name: str | None = None
    phone_number: str | None = None


class ResetPasswordOut(CamelModel):
    temp_password: str


class AuditLogEntryOut(CamelModel):
    id: str
    actor_id: str
    actor_name: str | None = None
    action: str
    target_type: str
    target_id: str
    detail: str | None = None
    created_at: datetime


class UpgradeRequestSummaryOut(CamelModel):
    """One row in the Super Admin Upgrade Requests inbox - see
    UpgradeRequest's model docstring for why this exists as its own table
    rather than reading the audit log directly (queue state needs a
    status, an append-only log isn't the right shape for that).
    """

    id: str
    school_id: str
    school_name: str
    requested_by_name: str
    requested_by_email: str
    message: str | None
    status: UpgradeRequestStatus
    created_at: datetime
    resolved_at: datetime | None


class UpdateUpgradeRequestStatusIn(CamelModel):
    status: UpgradeRequestStatus
