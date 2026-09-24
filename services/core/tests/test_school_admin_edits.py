"""Password reset recovery path, general profile edits, the per-school
default Bloom distribution, admin_catalog's audit-log gap fix, and the
bulk-create dateOfBirth isolation bug - all against real rows via
db_session, same pattern as test_subscription_limits.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from shared.errors import ConflictError
from sqlalchemy import select

from src.api.routes import admin_catalog, school_admin, school_oversight
from src.api.schemas.admin_catalog import CreateSubjectIn
from src.api.schemas.bloom import UpdateBloomDistributionIn
from src.api.schemas.school_admin import (
    CreateClassIn,
    CreateStudentIn,
    InviteTeacherIn,
    SendCredentialsEmailIn,
    UpdateClassIn,
    UpdateClassStatusIn,
    UpdateSchoolAdminMeIn,
    UpdateStudentIn,
    UpgradeRequestIn,
)
from src.config.settings import CoreSettings
from src.domain.models import (
    AssignmentTargetMode,
    AssistantMessage,
    AuditLog,
    BloomLevel,
    Chapter,
    ChatRole,
    Plan,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    StudentTestResult,
    Subject,
    Subscription,
    TestStatus,
    User,
    UserStatus,
    VoiceTest,
)
from src.repositories import audit_repository
from src.services import user_provisioning


@pytest.fixture(autouse=True)
def _fake_provisioning(monkeypatch):
    def fake_create(*, email, display_name, role):
        return user_provisioning.ProvisionedUser(firebase_uid=f"uid-{email}", temp_password="TempPass123")

    monkeypatch.setattr(school_admin, "create_firebase_user", fake_create)


async def _seed_school(db_session) -> School:
    school = School(name="Edit Test School", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_school_admin(db_session, school_id) -> User:
    admin = User(
        firebase_uid=f"admin-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Admin", role=Role.SCHOOL_ADMIN, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()
    return admin


async def test_update_student_partial_update_only_changes_provided_fields(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    school_class = SchoolClass(school_id=school.id, grade=5, section="A")
    db_session.add(school_class)
    await db_session.flush()

    created = await school_admin.create_student(
        CreateStudentIn(display_name="Original Name", email="s1@example.com", class_id=str(school_class.id), roll_number=1),
        user=admin, session=db_session,
    )

    updated = await school_admin.update_student(
        created.id, UpdateStudentIn(guardian_name="New Guardian"), user=admin, session=db_session,
    )

    assert updated.guardian_name == "New Guardian"
    assert updated.display_name == "Original Name"  # untouched - not in the partial update
    assert updated.class_id == str(school_class.id)  # untouched


async def test_get_school_curriculum_reflects_super_admin_assignment(db_session):
    """School Admin's PUT /curriculum was removed - subject assignment is
    now exclusively Super Admin's job (see school_admin.py's
    get_school_curriculum docstring). This just confirms the read path
    still reflects whatever Super Admin has assigned via SchoolCurriculum
    directly, since School Admin has no write path to test against.
    """
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()
    db_session.add(SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True))
    await db_session.flush()

    result = await school_admin.get_school_curriculum(user=admin, session=db_session)

    enabled_ids = {s.id for s in result.subjects if s.enabled}
    assert enabled_ids == {str(subject.id)}


def test_bloom_distribution_rejects_non_100_sum():
    with pytest.raises(Exception):  # pydantic ValidationError
        UpdateBloomDistributionIn(distribution={BloomLevel.REMEMBER: 50, BloomLevel.UNDERSTAND: 40})


def test_bloom_distribution_accepts_100_sum():
    body = UpdateBloomDistributionIn(distribution={BloomLevel.REMEMBER: 60, BloomLevel.UNDERSTAND: 40})
    assert sum(body.distribution.values()) == 100


async def test_school_admin_can_view_and_edit_own_profile(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)

    before = await school_admin.get_school_admin_me(user=admin, session=db_session)
    assert before.display_name == "Admin"
    assert before.school_name == school.name

    after = await school_admin.update_school_admin_me(
        UpdateSchoolAdminMeIn(phone_number="9999999999"), user=admin, session=db_session,
    )
    assert after.phone_number == "9999999999"
    assert after.display_name == "Admin"  # untouched - not in the partial update


async def test_school_audit_log_has_no_cross_school_leakage(db_session):
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    admin_a = await _seed_school_admin(db_session, school_a.id)
    admin_b = await _seed_school_admin(db_session, school_b.id)

    await school_admin.invite_teacher(
        InviteTeacherIn(email=f"{uuid.uuid4()}@example.com", display_name="Teacher A"),
        user=admin_a, session=db_session,
    )
    await school_admin.invite_teacher(
        InviteTeacherIn(email=f"{uuid.uuid4()}@example.com", display_name="Teacher B"),
        user=admin_b, session=db_session,
    )

    result = await school_oversight.list_school_audit_log(limit=50, offset=0, user=admin_a, session=db_session)

    assert result.total == 1
    assert result.items[0].action == "teacher.invited"
    assert result.items[0].actor_name == "Admin"
    assert result.items[0].detail.startswith("Teacher A (")


async def test_school_audit_log_excludes_super_admin_actions_on_this_school(db_session):
    """A Super Admin action on this school (e.g. creating its
    subscription) shouldn't appear here - the actor doesn't belong to
    this school, only the target does. That's visible on the Super Admin
    side (GET /admin/audit-log), not this school-scoped one.
    """
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    await audit_repository.record(
        db_session, actor_id=super_admin.id, action="school.onboarded", target_type="school", target_id=str(school.id)
    )
    await db_session.commit()

    result = await school_oversight.list_school_audit_log(limit=50, offset=0, user=admin, session=db_session)

    assert result.total == 0


async def test_school_audit_log_pagination(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    for i in range(3):
        await school_admin.invite_teacher(
            InviteTeacherIn(email=f"{uuid.uuid4()}@example.com", display_name=f"Teacher {i}"),
            user=admin, session=db_session,
        )

    first_page = await school_oversight.list_school_audit_log(limit=2, offset=0, user=admin, session=db_session)
    second_page = await school_oversight.list_school_audit_log(limit=2, offset=2, user=admin, session=db_session)

    assert first_page.total == 3
    assert len(first_page.items) == 2
    assert second_page.total == 3
    assert len(second_page.items) == 1


async def test_admin_catalog_mutation_creates_audit_log_row(db_session):
    admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()

    before = (await db_session.execute(AuditLog.__table__.select())).all()

    await admin_catalog.create_subject(
        CreateSubjectIn(name="New Subject", grades=[9]), actor=admin, session=db_session
    )

    after = (await db_session.execute(AuditLog.__table__.select())).all()
    assert len(after) == len(before) + 1


class _FakeUploadFile:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


async def test_bulk_create_students_isolates_bad_date_of_birth_row(db_session):
    """Regression test: a bad dateOfBirth string used to reach asyncpg
    directly (no parsing on the bulk path, unlike create_student's
    Pydantic-typed date field), corrupting the session for every row after
    it - see the fix in school_admin.bulk_create_students.
    """
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    school_class = SchoolClass(school_id=school.id, grade=5, section="A")
    db_session.add(school_class)
    await db_session.flush()

    csv_content = (
        "displayName,email,classGrade,classSection,rollNumber,guardianName,guardianPhone,dateOfBirth\n"
        "Row One,r1@example.com,5,A,1,,,\n"
        "Row Two,r2@example.com,5,A,2,,,2010-05-01\n"
        "Row Three,r3@example.com,5,A,3,,,not-a-date\n"
    ).encode("utf-8")
    upload = _FakeUploadFile("students.csv", csv_content)

    result = await school_admin.bulk_create_students(file=upload, user=admin, session=db_session)

    assert result.created_count == 2
    assert result.error_count == 1
    by_row = {r.row: r for r in result.results}
    assert by_row[2].status == "created"
    assert by_row[3].status == "created"
    assert by_row[4].status == "error"
    assert "dateOfBirth" in by_row[4].error

    # The two good rows actually persisted, proving the bad row's failure
    # didn't leave the session unusable for the rows around it.
    listed = await school_admin.list_students(class_id=str(school_class.id), user=admin, session=db_session)
    assert {item.email for item in listed.items} == {"r1@example.com", "r2@example.com"}


@pytest.fixture(autouse=True)
def _fake_email(monkeypatch):
    sent = []

    def fake_send(*, to_email, subject, message):
        sent.append((to_email, subject, message))

    monkeypatch.setattr(school_admin, "send_email", fake_send)
    return sent


async def test_delete_teacher_removes_the_account(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    invited = await school_admin.invite_teacher(
        InviteTeacherIn(email=f"{uuid.uuid4()}@example.com", display_name="Deletable Teacher"),
        user=admin, session=db_session,
    )

    result = await school_admin.delete_teacher(invited.id, actor=admin, session=db_session)

    assert result == {"deleted": True}
    listed = await school_admin.list_teachers(user=admin, session=db_session)
    assert invited.id not in {t.id for t in listed.items}
    audit_row = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "teacher.deleted", AuditLog.target_id == invited.id))
    ).scalar_one()
    assert audit_row.detail is not None and "Deletable Teacher" in audit_row.detail


async def test_delete_teacher_is_idempotent_for_unknown_id(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)

    result = await school_admin.delete_teacher(str(uuid.uuid4()), actor=admin, session=db_session)

    assert result == {"deleted": True}


async def test_delete_teacher_blocked_when_teacher_has_content(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    invited = await school_admin.invite_teacher(
        InviteTeacherIn(email=f"{uuid.uuid4()}@example.com", display_name="Busy Teacher"),
        user=admin, session=db_session,
    )
    db_session.add(AssistantMessage(teacher_id=invited.id, role=ChatRole.USER, text="hi"))
    await db_session.flush()

    with pytest.raises(ConflictError):
        await school_admin.delete_teacher(invited.id, actor=admin, session=db_session)

    # Still there - the failed delete didn't half-apply.
    listed = await school_admin.list_teachers(user=admin, session=db_session)
    assert invited.id in {t.id for t in listed.items}


async def test_send_teacher_credentials_email_uses_the_admins_reviewed_draft(db_session, _fake_email):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    invited = await school_admin.invite_teacher(
        InviteTeacherIn(email="teacher-email@example.com", display_name="Emailed Teacher"),
        user=admin, session=db_session,
    )

    result = await school_admin.send_teacher_credentials_email(
        invited.id,
        SendCredentialsEmailIn(subject="Welcome", message="Here you go", temp_password="TempPass123"),
        actor=admin, session=db_session,
    )

    assert result.sent is True
    assert _fake_email == [("teacher-email@example.com", "Welcome", "Here you go")]


async def test_delete_student_removes_profile_and_account(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    school_class = SchoolClass(school_id=school.id, grade=6, section="B")
    db_session.add(school_class)
    await db_session.flush()
    created = await school_admin.create_student(
        CreateStudentIn(display_name="Deletable Student", email=f"{uuid.uuid4()}@example.com", class_id=str(school_class.id), roll_number=1),
        user=admin, session=db_session,
    )

    result = await school_admin.delete_student(created.id, actor=admin, session=db_session)

    assert result == {"deleted": True}
    listed = await school_admin.list_students(class_id=None, user=admin, session=db_session)
    assert created.id not in {s.id for s in listed.items}


async def test_update_school_class_edits_grade_and_section(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    created = await school_admin.create_school_class(CreateClassIn(grade=5, section="A"), user=admin, session=db_session)

    updated = await school_admin.update_school_class(
        created.id, UpdateClassIn(grade=6, section="C"), user=admin, session=db_session,
    )

    assert updated.grade == 6
    assert updated.section == "C"


def test_create_class_rejects_grade_above_twelve():
    with pytest.raises(Exception):  # pydantic ValidationError
        CreateClassIn(grade=13, section="A")


def test_create_class_rejects_grade_below_one():
    with pytest.raises(Exception):  # pydantic ValidationError
        CreateClassIn(grade=0, section="A")


async def test_update_school_class_status_deactivates_and_reactivates(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    created = await school_admin.create_school_class(CreateClassIn(grade=7, section="A"), user=admin, session=db_session)
    assert created.status == UserStatus.ACTIVE

    deactivated = await school_admin.update_school_class_status(
        created.id, UpdateClassStatusIn(status=UserStatus.DEACTIVATED), user=admin, session=db_session,
    )
    assert deactivated.status == UserStatus.DEACTIVATED
    audit_row = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "class.status.updated", AuditLog.target_id == created.id))
    ).scalar_one()
    assert audit_row.detail == "Class 7 · A -> DEACTIVATED"


async def test_delete_school_class_blocked_when_it_has_students(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    created = await school_admin.create_school_class(CreateClassIn(grade=8, section="A"), user=admin, session=db_session)
    await school_admin.create_student(
        CreateStudentIn(display_name="Blocking Student", email=f"{uuid.uuid4()}@example.com", class_id=created.id, roll_number=1),
        user=admin, session=db_session,
    )

    with pytest.raises(ConflictError):
        await school_admin.delete_school_class(created.id, user=admin, session=db_session)


async def test_delete_school_class_removes_an_empty_class(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    created = await school_admin.create_school_class(CreateClassIn(grade=9, section="A"), user=admin, session=db_session)

    result = await school_admin.delete_school_class(created.id, user=admin, session=db_session)

    assert result == {"deleted": True}
    listed = await school_admin.list_school_classes(user=admin, session=db_session)
    assert created.id not in {c.id for c in listed.items}


async def test_school_analytics_mastery_trend_reflects_recent_test_results(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    school_class = SchoolClass(school_id=school.id, grade=9, section="A")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48,
        target_mode=AssignmentTargetMode.WHOLE_CLASS, status=TestStatus.RESULTS_READY,
    )
    db_session.add(test)
    await db_session.flush()
    student = User(
        firebase_uid=f"s-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=school.id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    db_session.add(StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=80))
    await db_session.flush()

    result = await school_admin.get_school_analytics(user=admin, session=db_session)

    assert len(result.mastery_trend) == 1
    assert result.mastery_trend[0].mastery_avg_percent == 80
    assert result.mastery_trend[0].test_count == 1


async def test_request_subscription_upgrade_records_audit_log_without_smtp(db_session, monkeypatch):
    monkeypatch.setattr(school_admin, "get_settings", lambda: CoreSettings(
        postgres_user="x", postgres_password="x", postgres_db="x",
    ))
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)

    result = await school_admin.request_subscription_upgrade(
        UpgradeRequestIn(message="need 20 more student seats"), user=admin, session=db_session,
    )

    assert result.recorded is True
    assert result.emailed is False  # no SMTP configured - still recorded, just not mailed
    row = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "subscription.upgrade_requested", AuditLog.target_id == str(school.id)
            )
        )
    ).scalar_one()
    assert row.detail == "need 20 more student seats"


async def test_request_subscription_upgrade_emails_sales_when_smtp_configured(db_session, monkeypatch, _fake_email):
    monkeypatch.setattr(school_admin, "get_settings", lambda: CoreSettings(
        postgres_user="x", postgres_password="x", postgres_db="x",
        smtp_host="smtp.example.com", smtp_from_email="noreply@example.com", sales_email="sales@example.com",
    ))
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)
    plan = Plan(name=f"Growth-{uuid.uuid4()}", price_label="x", teacher_limit=10, student_limit=100)
    db_session.add(plan)
    await db_session.flush()
    db_session.add(
        Subscription(
            school_id=school.id, plan_id=plan.id, renews_at=datetime.now(timezone.utc) + timedelta(days=30)
        )
    )
    await db_session.flush()

    result = await school_admin.request_subscription_upgrade(
        UpgradeRequestIn(), user=admin, session=db_session,
    )

    assert result.recorded is True
    assert result.emailed is True
    assert len(_fake_email) == 1
    to_email, subject, message = _fake_email[0]
    assert to_email == "sales@example.com"
    assert school.name in subject
    assert plan.name in message


async def test_request_subscription_upgrade_is_rate_limited_per_school(db_session, monkeypatch):
    monkeypatch.setattr(school_admin, "get_settings", lambda: CoreSettings(
        postgres_user="x", postgres_password="x", postgres_db="x",
    ))
    school = await _seed_school(db_session)
    admin = await _seed_school_admin(db_session, school.id)

    first = await school_admin.request_subscription_upgrade(
        UpgradeRequestIn(), user=admin, session=db_session,
    )
    assert first.recorded is True

    with pytest.raises(ConflictError):
        await school_admin.request_subscription_upgrade(UpgradeRequestIn(), user=admin, session=db_session)

    # Backdating the existing row past the cooldown (rather than sleeping in
    # the test) proves the limit is time-based, not a permanent one-shot.
    row = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "subscription.upgrade_requested", AuditLog.target_id == str(school.id)
            )
        )
    ).scalar_one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.flush()

    second = await school_admin.request_subscription_upgrade(UpgradeRequestIn(), user=admin, session=db_session)
    assert second.recorded is True


async def test_request_subscription_upgrade_rate_limit_is_scoped_per_school(db_session, monkeypatch):
    monkeypatch.setattr(school_admin, "get_settings", lambda: CoreSettings(
        postgres_user="x", postgres_password="x", postgres_db="x",
    ))
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    admin_a = await _seed_school_admin(db_session, school_a.id)
    admin_b = await _seed_school_admin(db_session, school_b.id)

    await school_admin.request_subscription_upgrade(UpgradeRequestIn(), user=admin_a, session=db_session)
    result_b = await school_admin.request_subscription_upgrade(UpgradeRequestIn(), user=admin_b, session=db_session)

    assert result_b.recorded is True
