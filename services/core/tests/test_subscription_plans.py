"""Core / Plus / Pro plan rules: spoken answers in daily practice (and how
many days a week), and Pro's extra challenge a month on a taken chapter."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.errors import ConflictError
from src.domain.models import DailyPracticeItem, DailyPracticeSession, Plan, QuizTest, Subscription
from src.domain.models import BloomLevel as B
from src.services import challenge_rules
from src.services import daily_practice as service
from tests.test_daily_practice import _pool, _seed


async def _subscribe(db, school, **plan_fields):
    plan = Plan(name=f"Plan-{uuid.uuid4()}", price_label="x", teacher_limit=10, student_limit=100, **plan_fields)
    db.add(plan)
    await db.flush()
    db.add(Subscription(school_id=school.id, plan_id=plan.id, renews_at=datetime.now(timezone.utc) + timedelta(days=30)))
    await db.flush()


@pytest.mark.asyncio
async def test_spoken_answers_follow_the_plan(db_session):
    s = await _seed(db_session)
    today = service.today_ist()
    assert await service.spoken_allowed(db_session, s.student, today)  # no plan yet: everything on

    await _subscribe(db_session, s.school, enabled_features=["daily_practice"])  # Core-like: no spoken answers
    assert not await service.spoken_allowed(db_session, s.student, today)


@pytest.mark.asyncio
async def test_spoken_days_a_week(db_session):
    s = await _seed(db_session)
    today = service.today_ist()
    await _subscribe(db_session, s.school, enabled_features=["daily_practice", "spoken_answers"],
                     spoken_days_per_week=1)
    assert await service.spoken_allowed(db_session, s.student, today)
    if today.weekday() == 0:
        return  # Monday: no earlier day this week to have used it on
    voice = await _pool(db_session, s.current, 1, kind="voice")
    past = DailyPracticeSession(student_id=s.student.id, chapter_id=s.electricity.id, status="completed",
                                practice_date=today - timedelta(days=1))
    db_session.add(past)
    await db_session.flush()
    db_session.add(DailyPracticeItem(session_id=past.id, order=1, practice_question_id=voice[0].id, kind="voice",
                                     topic_id=s.current.id, bloom_level=B.UNDERSTAND))
    await db_session.flush()
    assert not await service.spoken_allowed(db_session, s.student, today)


async def _fix_milo(db, s, created_at):
    db.add(QuizTest(class_id=s.cls.id, subject_id=s.electricity.subject_id, chapter_id=s.electricity.id,
                    title="Fix Milo", status="published", created_by=s.teacher.id, created_at=created_at))
    await db.flush()


@pytest.mark.asyncio
async def test_one_per_chapter_unless_the_plan_has_extras(db_session):
    s = await _seed(db_session)
    await _fix_milo(db_session, s, datetime.now(timezone.utc) - timedelta(days=60))
    await _subscribe(db_session, s.school, extra_challenges_per_month=1)

    # Pro: one more this month on the taken chapter...
    await challenge_rules.ensure_one_per_chapter(db_session, s.cls.id, s.electricity.id, "fix_steps")
    await _fix_milo(db_session, s, datetime.now(timezone.utc))
    # ...and then the month's extra is used
    with pytest.raises(ConflictError):
        await challenge_rules.ensure_one_per_chapter(db_session, s.cls.id, s.electricity.id, "fix_steps")


@pytest.mark.asyncio
async def test_one_per_chapter_without_extras(db_session):
    s = await _seed(db_session)
    await _fix_milo(db_session, s, datetime.now(timezone.utc) - timedelta(days=60))
    await _subscribe(db_session, s.school)
    with pytest.raises(ConflictError):
        await challenge_rules.ensure_one_per_chapter(db_session, s.cls.id, s.electricity.id, "fix_steps")


@pytest.mark.asyncio
async def test_school_discount_is_stored_and_bounded(db_session):
    from pydantic import ValidationError

    from src.api.routes.admin import update_school_subscription
    from src.api.schemas.admin import UpdateSubscriptionIn

    s = await _seed(db_session)
    await _subscribe(db_session, s.school)
    out = await update_school_subscription(str(s.school.id), UpdateSubscriptionIn(discount_percent=10),
                                           actor=s.teacher, session=db_session)
    assert out.discount_percent == 10
    out = await update_school_subscription(str(s.school.id), UpdateSubscriptionIn(status=None),
                                           actor=s.teacher, session=db_session)
    assert out.discount_percent == 10  # left alone when not sent
    with pytest.raises(ValidationError):
        UpdateSubscriptionIn(discount_percent=101)
