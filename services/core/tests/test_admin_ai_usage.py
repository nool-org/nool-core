"""Super Admin's AI usage view (routes/admin_ai_usage.py) - real rows against
the dev Postgres via db_session (rolled back), each test scoped to a fresh
school so the dev DB's own usage rows don't leak into the numbers."""

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shared.errors import ValidationError
from src.api.deps import get_current_app_user
from src.api.routes import admin_ai_usage
from src.domain.models import AiUsageEvent, Role, School, User, UserStatus

IST = timezone(timedelta(hours=5, minutes=30))
JAN1, JAN3 = date(2026, 1, 1), date(2026, 1, 3)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=IST)


async def _user(db_session, role: Role, school_id, name: str) -> User:
    user = User(firebase_uid=f"uid-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=name,
                role=role, school_id=school_id, status=UserStatus.ACTIVE)
    db_session.add(user)
    await db_session.flush()
    return user


async def _seed(db_session):
    school = School(name=f"AI School {uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    other = School(name=f"Other {uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    db_session.add_all([school, other])
    await db_session.flush()
    asha = await _user(db_session, Role.STUDENT, school.id, "Asha")
    bala = await _user(db_session, Role.STUDENT, school.id, "Bala")
    teacher = await _user(db_session, Role.TEACHER, school.id, "Mrs Rao")

    def ev(at, feature, model, inr, latency, status=200, student=None, teacher_id=None, models=None,
           school_id=school.id, error=None, endpoint="/v1/x"):
        return AiUsageEvent(created_at=at, service="generation", endpoint=endpoint, feature=feature, model=model,
                            models=models if models is not None else ([model] if model else []), calls=1,
                            input_tokens=1000, output_tokens=100, usd=inr / 86.5, inr=inr, latency_ms=latency,
                            status=status, error=error, school_id=school_id, teacher_id=teacher_id,
                            student_id=student.id if student else None)

    events = [
        ev(_at(1, 10), "homework", "gpt-5.1", 10, 100, student=asha, models=["gpt-5.1", "gpt-5-mini"]),
        ev(_at(1, 12), "practice_grade", "gpt-5-mini", 2, 200, student=asha),
        # 20:00 UTC on Jan 1 is 01:30 IST on Jan 2
        ev(datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc), "practice_grade", "gpt-5-mini", 3, 300, status=503,
           student=bala, error="HTTP 503 GRADING_UNAVAILABLE"),
        ev(_at(3, 9), "voice_test", "gpt-realtime-2.1-mini", 5, None, student=bala),
        ev(_at(3, 10), "assistant", None, 4, 400, status=0, teacher_id=teacher.id, error="ConnectError: refused",
           endpoint="/v1/teacher-assistant/reply"),
        ev(_at(4, 10), "homework", "gpt-5.1", 100, 100, student=asha),  # after the range
        ev(_at(2, 10), "homework", "gpt-5.1", 100, 100, school_id=other.id),  # another school
    ]
    db_session.add_all(events)
    await db_session.flush()
    return SimpleNamespace(school=school, other=other, asha=asha, bala=bala, teacher=teacher, events=events)


async def _summary(db_session, seed, **kw):
    args = {"from_": JAN1, "to": JAN3, "school_id": seed.school.id, "feature": None, "model": None}
    return await admin_ai_usage.get_ai_usage_summary(**{**args, **kw}, _=None, session=db_session)


async def test_summary_totals_percentiles_and_cost_per_active_student(db_session):
    seed = await _seed(db_session)
    out = await _summary(db_session, seed)
    t = out.totals
    assert (out.range.from_, out.range.to) == (JAN1, JAN3)
    assert (t.requests, t.errors, t.error_rate) == (5, 2, 0.4)
    assert t.cost_inr == 24 and t.cost_usd == pytest.approx(24 / 86.5, abs=1e-5)
    assert (t.input_tokens, t.output_tokens) == (5000, 500)
    assert (t.p50_latency_ms, t.p95_latency_ms) == (250, 385)  # percentile_cont over 100..400; null skipped
    assert t.active_students == 2
    assert t.cost_per_active_student_inr == 12 and t.cost_per_active_student_per_day_inr == 4  # 24/2, /3 days


async def test_summary_by_day_is_ist_and_fills_empty_days(db_session):
    seed = await _seed(db_session)
    days = (await _summary(db_session, seed)).by_day
    assert [(d.date, d.cost_inr, d.requests, d.errors, d.by_feature) for d in days] == [
        (date(2026, 1, 1), 12, 2, 0, {"homework": 10, "practice_grade": 2}),
        (date(2026, 1, 2), 3, 1, 1, {"practice_grade": 3}),
        (date(2026, 1, 3), 9, 2, 1, {"voice_test": 5, "assistant": 4}),
    ]
    out = await _summary(db_session, seed, from_=date(2025, 12, 31), to=JAN1)
    assert [(d.date, d.requests) for d in out.by_day] == [(date(2025, 12, 31), 0), (JAN1, 2)]


async def test_summary_breakdowns(db_session):
    seed = await _seed(db_session)
    out = await _summary(db_session, seed)
    assert [(f.feature, f.requests, f.cost_inr, f.share, f.avg_latency_ms, f.error_rate) for f in out.by_feature] == [
        ("homework", 1, 10, 0.4167, 100, 0), ("practice_grade", 2, 5, 0.2083, 250, 0.5),
        ("voice_test", 1, 5, 0.2083, None, 0), ("assistant", 1, 4, 0.1667, 400, 1)]
    assert [(m.model, m.requests, m.cost_inr) for m in out.by_model] == [
        ("gpt-5.1", 1, 10), ("gpt-5-mini", 2, 5), ("gpt-realtime-2.1-mini", 1, 5), (None, 1, 4)]
    [school] = out.by_school
    assert (school.school_id, school.school_name, school.requests, school.cost_inr, school.active_students,
            school.cost_per_student_inr) == (str(seed.school.id), seed.school.name, 5, 24, 2, 12)
    assert [(s.student_id, s.name, s.school_name, s.requests, s.cost_inr) for s in out.top_students] == [
        (str(seed.asha.id), "Asha", seed.school.name, 2, 12), (str(seed.bala.id), "Bala", seed.school.name, 2, 8)]


async def test_summary_filters_by_feature_and_by_any_model_used(db_session):
    seed = await _seed(db_session)
    by_model = await _summary(db_session, seed, model="gpt-5-mini")  # homework used it as its verifier
    assert (by_model.totals.requests, by_model.totals.cost_inr) == (3, 15)
    by_feature = await _summary(db_session, seed, feature="practice_grade")
    assert (by_feature.totals.requests, by_feature.totals.errors, by_feature.totals.active_students) == (2, 1, 2)
    # no school filter: both schools, one row each
    everything = await _summary(db_session, seed, school_id=None, feature="homework", from_=JAN1, to=JAN3)
    assert {s.school_id for s in everything.by_school} >= {str(seed.school.id), str(seed.other.id)}


async def test_summary_budget_from_settings_and_projection(db_session, monkeypatch):
    seed = await _seed(db_session)
    out = await _summary(db_session, seed)
    assert out.budget.monthly_inr is None  # unset
    monkeypatch.setenv("AI_MONTHLY_BUDGET_INR", "5000")
    db_session.add(AiUsageEvent(service="generation", endpoint="/v1/x", feature="homework", status=200, inr=7))
    await db_session.flush()
    out = await _summary(db_session, seed)
    assert out.budget.monthly_inr == 5000 and out.budget.spent_this_month_inr >= 7  # platform-wide, this month
    # 300 spent by the 10th of a 30-day month -> 900
    assert admin_ai_usage.projected_month(300, date(2026, 9, 10)) == 900
    assert admin_ai_usage.projected_month(0, date(2026, 2, 1)) == 0


async def test_summary_rejects_a_backwards_range(db_session):
    with pytest.raises(ValidationError):
        await admin_ai_usage.get_ai_usage_summary(from_=JAN3, to=JAN1, school_id=None, feature=None, model=None,
                                                  _=None, session=db_session)


async def _events(db_session, seed, **kw):
    args = {"from_": JAN1, "to": JAN3, "school_id": seed.school.id, "feature": None, "model": None, "status": None,
            "q": None, "cursor": None, "limit": 50}
    return await admin_ai_usage.list_ai_usage_events(**{**args, **kw}, _=None, session=db_session)


async def test_events_newest_first_with_names_and_keyset_pages(db_session):
    seed = await _seed(db_session)
    everything = await _events(db_session, seed)
    assert [e.feature for e in everything.items] == ["assistant", "voice_test", "practice_grade", "practice_grade",
                                                     "homework"]
    assert everything.next_cursor is None
    first = everything.items[0]
    assert (first.teacher_name, first.school_name, first.student_name, first.status, first.cost_inr) == (
        "Mrs Rao", seed.school.name, None, 0, 4)

    seen, cursor = [], None
    for _ in range(3):
        page = await _events(db_session, seed, limit=2, cursor=cursor)
        seen += [e.id for e in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [e.id for e in everything.items] and cursor is None


async def test_events_filters(db_session):
    seed = await _seed(db_session)
    errors = await _events(db_session, seed, status="error")
    assert {e.feature for e in errors.items} == {"assistant", "practice_grade"} and len(errors.items) == 2
    assert len((await _events(db_session, seed, status="ok")).items) == 3
    assert [e.student_name for e in (await _events(db_session, seed, q="bal")).items] == ["Bala", "Bala"]
    assert [e.feature for e in (await _events(db_session, seed, q="teacher-assistant")).items] == ["assistant"]
    assert len((await _events(db_session, seed, q="100%")).items) == 0  # % is literal, not a wildcard
    assert [e.feature for e in (await _events(db_session, seed, model="gpt-5-mini")).items] == [
        "practice_grade", "practice_grade", "homework"]
    assert [e.feature for e in (await _events(db_session, seed, feature="voice_test")).items] == ["voice_test"]
    with pytest.raises(ValidationError):
        await _events(db_session, seed, cursor="not-a-cursor")


async def test_filters_lists_features_models_and_schools(db_session):
    seed = await _seed(db_session)
    out = await admin_ai_usage.get_ai_usage_filters(_=None, session=db_session)
    assert {"homework", "practice_grade", "voice_test", "assistant"} <= set(out.features)
    assert {"gpt-5.1", "gpt-5-mini", "gpt-realtime-2.1-mini"} <= set(out.models)
    assert {"id": str(seed.school.id), "name": seed.school.name} in [s.model_dump() for s in out.schools]


@pytest.mark.parametrize("path", ["summary", "events", "filters"])
@pytest.mark.parametrize("role", [Role.SCHOOL_ADMIN, Role.TEACHER, Role.STUDENT])
def test_only_super_admin(_core_env, path, role):
    from src.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_app_user] = lambda: User(
        id=uuid.uuid4(), firebase_uid="u", email="e@x.com", display_name="X", role=role, school_id=uuid.uuid4(),
        status=UserStatus.ACTIVE)
    response = TestClient(app).get(f"/api/v1/admin/ai-usage/{path}", headers={"Authorization": "Bearer x"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN"


def test_requires_authentication(client):
    assert client.get("/api/v1/admin/ai-usage/summary").status_code == 401
