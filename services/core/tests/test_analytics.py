"""Platform-wide mastery trend (GET /admin/analytics/platform) and the
cross-tenant per-school analytics view (GET /admin/schools/{id}/analytics)
- both real, computed-at-read-time from StudentTestResult/VoiceTest rows,
no snapshot table. Real rows against the dev Postgres via db_session.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.errors import NotFoundError

from src.api.routes import admin
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    Role,
    School,
    SchoolClass,
    StudentProfile,
    StudentTestResult,
    Subject,
    TestStatus,
    User,
    UserStatus,
    VoiceTest,
)
from src.services.school_analytics import compute_school_analytics


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


async def _seed_test_result(db_session, school_id, *, mastery_percent: int) -> None:
    school_class = SchoolClass(school_id=school_id, grade=9, section=uuid.uuid4().hex[:8].upper())
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
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    db_session.add(StudentProfile(user_id=student.id, class_id=school_class.id, roll_number=1))

    db_session.add(
        StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=mastery_percent)
    )
    await db_session.flush()


async def test_platform_analytics_includes_a_real_mastery_trend(db_session):
    school = await _seed_school(db_session)
    admin_user = await _seed_super_admin(db_session)
    await _seed_test_result(db_session, school.id, mastery_percent=80)

    result = await admin.get_platform_analytics(_=admin_user, session=db_session)

    assert result.mastery_trend  # at least one week of real data
    assert result.mastery_trend[-1].test_count >= 1


async def test_super_admin_can_view_a_specific_schools_analytics(db_session):
    school = await _seed_school(db_session)
    admin_user = await _seed_super_admin(db_session)
    await _seed_test_result(db_session, school.id, mastery_percent=70)

    result = await admin.get_school_analytics_as_admin(str(school.id), _=admin_user, session=db_session)

    assert result.school_mastery_avg_percent == 70
    assert len(result.class_breakdown) == 1


async def test_school_analytics_404s_for_a_nonexistent_school(db_session):
    admin_user = await _seed_super_admin(db_session)

    with pytest.raises(NotFoundError):
        await admin.get_school_analytics_as_admin(str(uuid.uuid4()), _=admin_user, session=db_session)


async def test_compute_school_analytics_never_leaks_across_schools(db_session):
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    await _seed_test_result(db_session, school_a.id, mastery_percent=90)
    await _seed_test_result(db_session, school_b.id, mastery_percent=10)

    result_a = await compute_school_analytics(db_session, school_a.id)
    result_b = await compute_school_analytics(db_session, school_b.id)

    assert result_a.school_mastery_avg_percent == 90
    assert result_b.school_mastery_avg_percent == 10


async def test_school_analytics_shows_improvement_weak_chapters_and_slipping_students(db_session):
    school = await _seed_school(db_session)
    school_class = SchoolClass(school_id=school.id, grade=10, section="A")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    student = User(firebase_uid=f"s-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="Asha",
                   role=Role.STUDENT, school_id=school.id, status=UserStatus.ACTIVE)
    db_session.add(student)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    for name, percent, when in (("Light", 80, now - timedelta(days=7)), ("Electricity", 30, now)):
        chapter = Chapter(subject_id=subject.id, name=name)
        db_session.add(chapter)
        await db_session.flush()
        test = VoiceTest(class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
                         duration_minutes=20, completion_window_hours=48, created_at=when,
                         target_mode=AssignmentTargetMode.WHOLE_CLASS, status=TestStatus.RESULTS_READY)
        db_session.add(test)
        await db_session.flush()
        db_session.add(StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=percent))
    await db_session.flush()

    result = await compute_school_analytics(db_session, school.id)

    row = result.class_breakdown[0]
    assert (row.improvement_percent, row.challenges_taken, row.students_assessed) == (-50, 2, 1)
    assert [c.chapter for c in result.weak_chapters] == ["Electricity", "Light"]
    assert result.weak_chapters[0].classes == ["Class 10 · A"]
    slip = result.slipping_students[0]
    assert (slip.name, slip.chapter, slip.latest_percent, slip.earlier_avg_percent) == ("Asha", "Electricity", 30, 80)
