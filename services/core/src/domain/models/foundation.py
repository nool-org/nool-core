import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin, TimestampMixin


class Role(str, Enum):
    """Matches root CLAUDE.md's "Initial roles" exactly."""

    SUPER_ADMIN = "SUPER_ADMIN"
    SCHOOL_ADMIN = "SCHOOL_ADMIN"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


class Feature(str, Enum):
    """The toggleable unit for School.enabled_features - one entry per
    product module with a real route surface, not one per endpoint. See
    that column's docstring and requirements.md §7.14 for the full
    registry rationale (e.g. why VOICE_TEST also gates the AI Assessor
    routes, not just voice_test.py).
    """

    QUESTION_PAPER = "question_paper"
    VOICE_TEST = "voice_test"
    HOMEWORK = "homework"
    ASSISTANT = "assistant"
    LEADERBOARD = "leaderboard"
    IMPROVEMENT_ANALYSIS = "improvement_analysis"


class UserStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"


class SchoolStatus(str, Enum):
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class SubscriptionStatus(str, Enum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELED = "CANCELED"


class Plan(IdMixin, Base):
    __tablename__ = "plans"

    name: Mapped[str] = mapped_column(String, unique=True)
    price_label: Mapped[str] = mapped_column(String)
    teacher_limit: Mapped[int] = mapped_column(Integer)
    student_limit: Mapped[int] = mapped_column(Integer)
    # NULL = unlimited. Enforced as a lifetime total count per school, not
    # per billing period - there is no billing-cycle infrastructure in this
    # schema yet (see docs/OBSERVABILITY.md-style judgment-call comments
    # elsewhere in this codebase for the pattern this follows).
    test_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    question_paper_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL = the plan bundles every Feature (the default for every plan
    # today). A non-null list is which modules this subscription tier
    # includes at all - the single source of truth for feature access
    # (deliberately not also settable per-school - see requirements.md
    # §7.14: a Platform Operator configures this on the plan/subscription
    # and it flows to every school on that plan, one control point, not
    # two). Enforced by src/api/deps.py's require_feature dependency, set
    # via GET/PUT /admin/plans/{id}/features.
    enabled_features: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Soft-disable so a retired plan doesn't break FK references from
    # schools/subscriptions still on it.
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class School(IdMixin, TimestampMixin, Base):
    __tablename__ = "schools"

    name: Mapped[str] = mapped_column(String)
    board: Mapped[str] = mapped_column(String)
    city: Mapped[str] = mapped_column(String)
    contact_email: Mapped[str] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    pincode: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    principal_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[SchoolStatus] = mapped_column(
        SAEnum(SchoolStatus, name="school_status"), default=SchoolStatus.ONBOARDING
    )
    # Deprecated as a source of truth - a school's current plan is always
    # resolved via its Subscription row (see
    # src/repositories/subscription_repository.py). Left in place rather
    # than dropped since it costs nothing to keep and dropping a column is
    # a destructive migration for no functional gain.
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id"), nullable=True
    )
    # NULL = use the system default. A mapping of Bloom level name -> target
    # percentage (must sum to 100, enforced in UpdateBloomDistributionIn's
    # validator, not here) - the concrete answer to "customize the
    # teacher-app options from admin," scoped to one option: the Bloom mix
    # Question Paper/Homework generation starts from (see
    # src/services/bloom_defaults.py). School Admin sets their own via
    # PUT /school/curriculum/default-bloom-distribution; Super Admin can
    # set/override any school's via PUT /admin/schools/{id}/default-bloom-distribution.
    default_bloom_distribution: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    # NULL = no logo set. A base64 `data:image/...` URI stored directly -
    # same precedent as QuestionPaper.logo_data_uri (no S3/object storage
    # anywhere in this codebase). School Admin sets their own via
    # PUT /school/logo; Super Admin can set/override any school's via
    # PUT /admin/schools/{id}/logo. Displayed across every app.
    logo_data_uri: Mapped[str | None] = mapped_column(Text, nullable=True)


class Subscription(IdMixin, Base):
    __tablename__ = "subscriptions"

    school_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schools.id"), unique=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"))
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, name="subscription_status"), default=SubscriptionStatus.TRIAL
    )
    renews_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UpgradeRequestStatus(str, Enum):
    PENDING = "PENDING"
    CONTACTED = "CONTACTED"
    RESOLVED = "RESOLVED"


class UpgradeRequest(IdMixin, TimestampMixin, Base):
    """A durable, queryable record of School Admin's "Request an upgrade"
    CTA (POST /school/subscription/upgrade-request) - that route always
    also wrote an audit-log row and best-effort emailed sales_email, but
    nothing on the Super Admin side could see these short of reading raw
    audit-log rows (which don't carry a resolved/unresolved state - an
    append-only log isn't the right shape for queue state). This table is
    the real inbox: GET/PATCH /admin/upgrade-requests.

    Not backfilled from pre-existing audit-log rows - this starts tracking
    from the point this table was introduced forward, not retroactively.
    """

    __tablename__ = "upgrade_requests"

    school_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schools.id"))
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[UpgradeRequestStatus] = mapped_column(
        SAEnum(UpgradeRequestStatus, name="upgrade_request_status"), default=UpgradeRequestStatus.PENDING
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)


class User(IdMixin, TimestampMixin, Base):
    """Application-level identity. firebase_uid is nullable-until-first-login:
    School Admin's teacher/student invite flow creates this row by email
    before the invitee has ever signed in to Firebase - see root CLAUDE.md's
    Identity section (Firebase UID is an external reference, never the
    primary key).
    """

    __tablename__ = "users"

    firebase_uid: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[Role] = mapped_column(SAEnum(Role, name="role"))
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schools.id"), nullable=True
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status"), default=UserStatus.ACTIVE
    )
    phone_number: Mapped[str | None] = mapped_column(String, nullable=True)
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # True from account creation until the invitee changes their temp
    # password - see src/services/user_provisioning.py and
    # POST /api/v1/me/acknowledge-password-change. Mirrored onto the
    # Firebase custom claim of the same name for clients that only ever
    # call the claim-only Auth service /me (nool-apps).
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(IdMixin, TimestampMixin, Base):
    """Every state-changing Super Admin action, across every school - see
    GET /api/v1/admin/audit-log.
    """

    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String)
    target_type: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
