"""Super Admin's AI usage view: what every AI request cost, by day, feature,
model, school and student - over ai_usage_events (written by
src/services/ai_usage.py). Dates are IST calendar days; the default range is
the last 30 days including today.
"""

import base64
import calendar
import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import distinct, func, literal_column, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from shared.errors import ValidationError

from src.api.deps import get_db_session, require_role
from src.api.schemas.admin_ai_usage import (
    AiUsageBudgetOut,
    AiUsageDayOut,
    AiUsageEventOut,
    AiUsageEventsOut,
    AiUsageFeatureOut,
    AiUsageFiltersOut,
    AiUsageModelOut,
    AiUsageRangeOut,
    AiUsageSchoolOptionOut,
    AiUsageSchoolOut,
    AiUsageServiceOut,
    AiUsageTeacherOut,
    AiUsageWastedOut,
    AiUsageStudentOut,
    AiUsageSummaryOut,
    AiUsageTotalsOut,
)
from src.config.settings import get_settings
from src.domain.models import AiUsageEvent as E
from src.domain.models import Role, School, User

router = APIRouter(prefix="/api/v1/admin/ai-usage", tags=["admin-ai-usage"])

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DAYS = 30
MAX_RANGE_DAYS = 366
TOP_STUDENTS = 10

# literal (not a bind param) so the SELECT and GROUP BY expressions match
IST_DAY = func.date(func.timezone(literal_column("'Asia/Kolkata'"), E.created_at))
IS_ERROR = or_(E.status < 200, E.status >= 300)  # 0 = network error


def _today() -> date:
    return datetime.now(IST).date()


def _range(from_: date | None, to: date | None) -> tuple[date, date]:
    to = to or _today()
    from_ = from_ or to - timedelta(days=DEFAULT_DAYS - 1)
    if from_ > to:
        raise ValidationError("from: must be on or before to")
    if (to - from_).days >= MAX_RANGE_DAYS:
        raise ValidationError(f"from: the range can be at most {MAX_RANGE_DAYS} days")
    return from_, to


def _where(from_: date, to: date, school_id: uuid.UUID | None, feature: str | None, model: str | None) -> list:
    conds = [E.created_at >= datetime.combine(from_, time(), IST),
             E.created_at < datetime.combine(to + timedelta(days=1), time(), IST)]
    if school_id:
        conds.append(E.school_id == school_id)
    if feature:
        conds.append(E.feature == feature)
    if model:
        conds.append(E.models.contains([model]))  # any model the request used
    return conds


def _ratio(part: float, whole: float, digits: int = 4) -> float:
    return round(part / whole, digits) if whole else 0.0


def _int(value) -> int | None:
    return None if value is None else round(value)


def projected_month(spent: float, today: date) -> float:
    """This month's spend extrapolated: spent so far / days elapsed (incl. today) * days in month."""
    return round(spent / today.day * calendar.monthrange(today.year, today.month)[1], 4)


async def _budget(session: AsyncSession) -> AiUsageBudgetOut:
    """Platform-wide (ignores the view's filters) - the budget is one number for the whole platform."""
    today = _today()
    month_start = datetime.combine(today.replace(day=1), time(), IST)
    spent = float((await session.execute(
        select(func.coalesce(func.sum(E.inr), 0)).where(E.created_at >= month_start))).scalar_one())
    return AiUsageBudgetOut(monthly_inr=get_settings().ai_monthly_budget_inr, spent_this_month_inr=round(spent, 4),
                            projected_month_inr=projected_month(spent, today))


@router.get("/summary", summary="AI cost and usage: totals, by day/feature/model/school, top students, budget")
async def get_ai_usage_summary(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    school_id: uuid.UUID | None = Query(default=None, alias="schoolId"),
    feature: str | None = Query(default=None, max_length=50),
    model: str | None = Query(default=None, max_length=100),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> AiUsageSummaryOut:
    """Cost per active student = the range's total cost / distinct students
    with AI requests in it (all AI cost, teacher-side too, so it is the number
    to price a seat against); per day divides that by the range's days."""
    from_, to = _range(from_, to)
    where = _where(from_, to, school_id, feature, model)
    cost = func.coalesce(func.sum(E.inr), 0)

    t = (await session.execute(select(
        func.count(), func.count().filter(IS_ERROR), func.coalesce(func.sum(E.usd), 0), cost,
        func.coalesce(func.sum(E.input_tokens), 0), func.coalesce(func.sum(E.output_tokens), 0),
        func.percentile_cont(literal_column("0.5")).within_group(E.latency_ms),
        func.percentile_cont(literal_column("0.95")).within_group(E.latency_ms),
        func.count(distinct(E.student_id)),
    ).where(*where))).one()
    requests, errors, usd, inr, active = t[0], t[1], float(t[2]), float(t[3]), t[8]
    days = (to - from_).days + 1
    per_student = _ratio(inr, active)
    totals = AiUsageTotalsOut(
        requests=requests, errors=errors, error_rate=_ratio(errors, requests), cost_usd=round(usd, 6),
        cost_inr=round(inr, 4), input_tokens=t[4], output_tokens=t[5], p50_latency_ms=_int(t[6]),
        p95_latency_ms=_int(t[7]), active_students=active, cost_per_active_student_inr=per_student,
        cost_per_active_student_per_day_inr=round(per_student / days, 4),
    )

    by_day = {from_ + timedelta(days=i): AiUsageDayOut(date=from_ + timedelta(days=i), cost_inr=0, requests=0,
                                                         errors=0, by_feature={}) for i in range(days)}
    for day, feat, n, n_err, day_inr in (await session.execute(
        select(IST_DAY, E.feature, func.count(), func.count().filter(IS_ERROR), cost)
        .where(*where).group_by(IST_DAY, E.feature)
    )).all():
        row = by_day[day]
        row.requests += n
        row.errors += n_err
        row.cost_inr = round(row.cost_inr + float(day_inr), 4)
        row.by_feature[feat] = round(float(day_inr), 4)

    by_feature = [
        AiUsageFeatureOut(feature=feat, requests=n, cost_inr=round(float(c), 4), share=_ratio(float(c), inr),
                          avg_latency_ms=_int(lat), error_rate=_ratio(n_err, n), cost_usd=round(float(u), 6),
                          avg_cost_inr=_ratio(float(c), n))
        for feat, n, c, lat, n_err, u in (await session.execute(
            select(E.feature, func.count(), cost, func.avg(E.latency_ms), func.count().filter(IS_ERROR),
                   func.coalesce(func.sum(E.usd), 0))
            .where(*where).group_by(E.feature).order_by(cost.desc(), E.feature)
        )).all()
    ]
    by_model = [
        AiUsageModelOut(model=m, requests=n, cost_inr=round(float(c), 4), input_tokens=i, output_tokens=o)
        for m, n, c, i, o in (await session.execute(
            select(E.model, func.count(), cost, func.coalesce(func.sum(E.input_tokens), 0),
                   func.coalesce(func.sum(E.output_tokens), 0))
            .where(*where).group_by(E.model).order_by(cost.desc(), E.model)
        )).all()
    ]
    by_school = [
        AiUsageSchoolOut(school_id=str(sid) if sid else None, school_name=name, requests=n,
                         cost_inr=round(float(c), 4), active_students=students,
                         cost_per_student_inr=_ratio(float(c), students))
        for sid, name, n, c, students in (await session.execute(
            select(E.school_id, School.name, func.count(), cost, func.count(distinct(E.student_id)))
            .outerjoin(School, School.id == E.school_id)
            .where(*where).group_by(E.school_id, School.name).order_by(cost.desc())
        )).all()
    ]
    top_students = [
        AiUsageStudentOut(student_id=str(sid), name=name, school_name=school_name, requests=n,
                          cost_inr=round(float(c), 4))
        for sid, name, school_name, n, c in (await session.execute(
            select(E.student_id, User.display_name, func.max(School.name), func.count(), cost)
            .outerjoin(User, User.id == E.student_id)
            .outerjoin(School, School.id == E.school_id)
            .where(*where, E.student_id.is_not(None))
            .group_by(E.student_id, User.display_name).order_by(cost.desc(), E.student_id).limit(TOP_STUDENTS)
        )).all()
    ]
    by_service = [
        AiUsageServiceOut(service=svc, requests=n, cost_inr=round(float(c), 4), cost_usd=round(float(u), 6),
                          errors=n_err)
        for svc, n, c, u, n_err in (await session.execute(
            select(E.service, func.count(), cost, func.coalesce(func.sum(E.usd), 0), func.count().filter(IS_ERROR))
            .where(*where).group_by(E.service).order_by(cost.desc())
        )).all()
    ]
    top_teachers = [
        AiUsageTeacherOut(teacher_id=str(tid), name=name, school_name=school_name, requests=n,
                          cost_inr=round(float(c), 4))
        for tid, name, school_name, n, c in (await session.execute(
            select(E.teacher_id, User.display_name, func.max(School.name), func.count(), cost)
            .outerjoin(User, User.id == E.teacher_id)
            .outerjoin(School, School.id == E.school_id)
            .where(*where, E.teacher_id.is_not(None))
            .group_by(E.teacher_id, User.display_name).order_by(cost.desc(), E.teacher_id).limit(TOP_STUDENTS)
        )).all()
    ]
    w = (await session.execute(select(func.count(), cost).where(*where, IS_ERROR, E.inr > 0))).one()
    return AiUsageSummaryOut(
        range=AiUsageRangeOut(from_=from_, to=to), totals=totals, by_day=list(by_day.values()),
        by_feature=by_feature, by_model=by_model, by_school=by_school, top_students=top_students,
        budget=await _budget(session), by_service=by_service, top_teachers=top_teachers,
        wasted=AiUsageWastedOut(requests=w[0], cost_inr=round(float(w[1]), 4)),
    )


def _encode_cursor(created_at: datetime, event_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{event_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, event_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created_at), uuid.UUID(event_id)
    except Exception as exc:
        raise ValidationError("cursor: invalid") from exc


@router.get("/events", summary="AI requests, newest first (keyset pagination)")
async def list_ai_usage_events(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    school_id: uuid.UUID | None = Query(default=None, alias="schoolId"),
    feature: str | None = Query(default=None, max_length=50),
    model: str | None = Query(default=None, max_length=100),
    status: Literal["ok", "error"] | None = Query(default=None),
    service: Annotated[str | None, Query(max_length=20)] = None,
    student_id: Annotated[uuid.UUID | None, Query(alias="studentId")] = None,
    teacher_id: Annotated[uuid.UUID | None, Query(alias="teacherId")] = None,
    q: str | None = Query(default=None, max_length=100),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> AiUsageEventsOut:
    """`q` matches the endpoint, feature, model, error, school or student/teacher
    name. `nextCursor` (null on the last page) is passed back as `cursor`."""
    from_, to = _range(from_, to)
    student, teacher = aliased(User), aliased(User)
    stmt = (
        select(E, School.name, student.display_name, teacher.display_name)
        .outerjoin(School, School.id == E.school_id)
        .outerjoin(student, student.id == E.student_id)
        .outerjoin(teacher, teacher.id == E.teacher_id)
        .where(*_where(from_, to, school_id, feature, model))
    )
    if status:
        stmt = stmt.where(IS_ERROR if status == "error" else ~IS_ERROR)
    if service:
        stmt = stmt.where(E.service == service)
    if student_id:
        stmt = stmt.where(E.student_id == student_id)
    if teacher_id:
        stmt = stmt.where(E.teacher_id == teacher_id)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(*(col.ilike(like, escape="\\") for col in (
            E.endpoint, E.feature, E.model, E.error, School.name, student.display_name, teacher.display_name))))
    if cursor:
        stmt = stmt.where(tuple_(E.created_at, E.id) < tuple_(*_decode_cursor(cursor)))
    rows = (await session.execute(stmt.order_by(E.created_at.desc(), E.id.desc()).limit(limit + 1))).all()

    items = [
        AiUsageEventOut(
            id=str(e.id), created_at=e.created_at, service=e.service, endpoint=e.endpoint, feature=e.feature,
            model=e.model, school_name=school_name, student_name=student_name, teacher_name=teacher_name,
            input_tokens=e.input_tokens, output_tokens=e.output_tokens, cost_usd=float(e.usd),
            cost_inr=float(e.inr), latency_ms=e.latency_ms, status=e.status, error=e.error,
            models=list(e.models or []), cached_input_tokens=e.cached_input_tokens, request_id=e.request_id,
            calls=e.calls,
        )
        for e, school_name, student_name, teacher_name in rows[:limit]
    ]
    last = rows[limit - 1][0] if len(rows) > limit else None
    return AiUsageEventsOut(items=items, next_cursor=_encode_cursor(last.created_at, last.id) if last else None)


@router.get("/filters", summary="Options for the AI usage view's filters")
async def get_ai_usage_filters(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> AiUsageFiltersOut:
    features = (await session.execute(select(E.feature).distinct().order_by(E.feature))).scalars().all()
    models = (await session.execute(select(func.jsonb_array_elements_text(E.models)).distinct())).scalars().all()
    schools = (await session.execute(select(School.id, School.name).order_by(School.name))).all()
    services = (await session.execute(select(E.service).distinct().order_by(E.service))).scalars().all()
    return AiUsageFiltersOut(features=list(features), models=sorted(models), services=list(services),
                             schools=[AiUsageSchoolOptionOut(id=str(sid), name=name) for sid, name in schools])
