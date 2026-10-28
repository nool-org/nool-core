"""GET /me/assigned-tests and GET /me/dashboard's pending_tests - before
this fix, both queries filtered only on class-membership
(VoiceTestTargetStudent), never on the student's own completion, so a
Test the student already finished never left their "current"/pending
list. This was a second, independent cause behind "once test is done,
not going to next page" alongside the fixed Homework auto-generation.
"""

import uuid

from src.api.routes import assigned_test, student_dashboard
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    Role,
    School,
    SchoolClass,
    StudentTestResult,
    Subject,
    User,
    TestStatus,
    UserStatus,
    VoiceTest,
    VoiceTestTargetStudent,
)


async def _seed_student(db_session, school_id) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    return student


async def _seed_targeted_test(db_session, *, school_id, subject) -> VoiceTest:
    school_class = SchoolClass(school_id=school_id, grade=9, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add_all([school_class, chapter])
    await db_session.flush()
    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
        status=TestStatus.SCHEDULED,  # a DRAFT never reaches students
    )
    db_session.add(test)
    await db_session.flush()
    return test


async def test_assigned_tests_excludes_a_test_the_student_already_completed(db_session):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)

    pending = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    done = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    db_session.add_all(
        [
            VoiceTestTargetStudent(test_id=pending.id, student_id=student.id),
            VoiceTestTargetStudent(test_id=done.id, student_id=student.id),
            StudentTestResult(test_id=done.id, student_id=student.id, mastery_percent=100),
        ]
    )
    await db_session.flush()

    result = await assigned_test.list_my_tests(user=student, session=db_session)

    ids = {item.id for item in result.items}
    assert str(pending.id) in ids
    assert str(done.id) not in ids


async def test_assigned_test_by_id_still_works_after_completion(db_session):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)
    done = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    db_session.add_all(
        [
            VoiceTestTargetStudent(test_id=done.id, student_id=student.id),
            StudentTestResult(test_id=done.id, student_id=student.id, mastery_percent=100),
        ]
    )
    await db_session.flush()

    # Direct fetch by id is intentionally not filtered - a student who
    # navigates straight to a finished Test (e.g. from its Results) can
    # still open it.
    fetched = await assigned_test.get_my_test(test_id=str(done.id), user=student, session=db_session)
    assert fetched.id == str(done.id)


async def test_dashboard_pending_tests_excludes_a_completed_test(db_session):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)

    pending = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    done = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    db_session.add_all(
        [
            VoiceTestTargetStudent(test_id=pending.id, student_id=student.id),
            VoiceTestTargetStudent(test_id=done.id, student_id=student.id),
            StudentTestResult(test_id=done.id, student_id=student.id, mastery_percent=100),
        ]
    )
    await db_session.flush()

    dashboard = await student_dashboard.get_student_dashboard(user=student, session=db_session)

    titles = [item.subject_label for item in dashboard.subject_today]
    assert subject.name in titles  # the pending one is still there
    # Only one row for this subject - the completed Test's row is gone,
    # not just relabeled (both tests share the same subject/label).
    assert titles.count(subject.name) == 1


async def test_an_expired_test_leaves_the_list_and_cannot_be_started(db_session):
    from datetime import datetime, timedelta, timezone

    import pytest

    from shared.errors import ConflictError
    from src.api.routes import ai_assessor
    from src.api.schemas.ai_assessor import OpenSessionIn

    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)
    now = datetime.now(timezone.utc)
    open_test = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    expired = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    open_test.scheduled_at = now - timedelta(hours=47)  # 48 h window: 1 h left
    expired.scheduled_at = now - timedelta(hours=49)
    db_session.add_all([VoiceTestTargetStudent(test_id=t.id, student_id=student.id) for t in (open_test, expired)])
    await db_session.flush()

    items = (await assigned_test.list_my_tests(user=student, session=db_session)).items
    assert [i.id for i in items] == [str(open_test.id)]
    assert abs((items[0].due_at - (now + timedelta(hours=1))).total_seconds()) < 5

    with pytest.raises(ConflictError, match="deadline"):
        await ai_assessor.open_session(
            OpenSessionIn(test_id=str(expired.id), context_label="x", bloom_levels=[], duration_seconds=600),
            app_user=student, db=db_session)


async def test_a_test_scheduled_for_later_stays_hidden_and_cannot_be_started(db_session):
    from datetime import datetime, timedelta, timezone

    import pytest

    from shared.errors import ConflictError
    from src.api.routes import ai_assessor
    from src.api.schemas.ai_assessor import OpenSessionIn
    from src.services import student_tasks

    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)
    later = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    later.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=20)
    db_session.add(VoiceTestTargetStudent(test_id=later.id, student_id=student.id))
    await db_session.flush()

    assert (await assigned_test.list_my_tests(user=student, session=db_session)).items == []
    tasks = await student_tasks._test_tasks(db_session, student, datetime.now(timezone.utc))
    assert tasks == []
    with pytest.raises(ConflictError, match="hasn't opened"):
        await ai_assessor.open_session(
            OpenSessionIn(test_id=str(later.id), context_label="x", bloom_levels=[], duration_seconds=600),
            app_user=student, db=db_session)


async def test_assigned_test_says_how_many_questions(db_session):
    """V-003: the ready check shows '3 questions' before the student taps I'm ready."""
    from src.domain.models import BloomLevel, VoiceTestQuestion

    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)
    test = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    db_session.add(VoiceTestTargetStudent(test_id=test.id, student_id=student.id))
    await db_session.flush()
    unprepared = await assigned_test.get_my_test(test_id=str(test.id), user=student, session=db_session)
    assert unprepared.question_count is None  # questions not written yet

    for order in (1, 2, 3):
        db_session.add(VoiceTestQuestion(voice_test_id=test.id, order=order, bloom_level=BloomLevel.REMEMBER,
                                         concepts=[], original_text="Q", reworded_text="Q?", spoken_text="Q",
                                         expected_points=[]))
    await db_session.flush()
    fetched = await assigned_test.get_my_test(test_id=str(test.id), user=student, session=db_session)
    assert fetched.question_count == 3


async def test_assigned_test_says_when_a_scheduled_test_opens(db_session):
    """V-007: a test scheduled for later says when it opens, on the ready check."""
    from datetime import datetime, timedelta, timezone

    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    student = await _seed_student(db_session, school.id)
    test = await _seed_targeted_test(db_session, school_id=school.id, subject=subject)
    opens = datetime.now(timezone.utc) + timedelta(days=1)
    test.scheduled_at = opens
    db_session.add(VoiceTestTargetStudent(test_id=test.id, student_id=student.id))
    await db_session.flush()

    later = await assigned_test.get_my_test(test_id=str(test.id), user=student, session=db_session)
    assert later.opens_at == opens

    test.scheduled_at = datetime.now(timezone.utc) - timedelta(hours=1)  # now open
    await db_session.flush()
    now_open = await assigned_test.get_my_test(test_id=str(test.id), user=student, session=db_session)
    assert now_open.opens_at is None
