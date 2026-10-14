from datetime import date, datetime

from pydantic import Field

from .common import CamelModel


class AiUsageRangeOut(CamelModel):
    from_: date = Field(alias="from")
    to: date


class AiUsageTotalsOut(CamelModel):
    requests: int
    errors: int
    error_rate: float  # 0-1
    cost_usd: float
    cost_inr: float
    input_tokens: int
    output_tokens: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    active_students: int  # distinct students with AI requests in the range
    cost_per_active_student_inr: float
    cost_per_active_student_per_day_inr: float


class AiUsageDayOut(CamelModel):
    date: date  # IST
    cost_inr: float
    requests: int
    errors: int
    by_feature: dict[str, float]  # feature -> cost INR


class AiUsageFeatureOut(CamelModel):
    feature: str
    requests: int
    cost_inr: float
    share: float  # of the range's total cost, 0-1
    avg_latency_ms: int | None
    error_rate: float
    cost_usd: float = 0.0
    avg_cost_inr: float = 0.0  # what one use costs: cost / requests


class AiUsageModelOut(CamelModel):
    model: str | None
    requests: int
    cost_inr: float
    input_tokens: int
    output_tokens: int


class AiUsageSchoolOut(CamelModel):
    school_id: str | None
    school_name: str | None
    requests: int
    cost_inr: float
    active_students: int
    cost_per_student_inr: float


class AiUsageStudentOut(CamelModel):
    student_id: str
    name: str | None  # None once the student is deleted (the cost history stays)
    school_name: str | None
    requests: int
    cost_inr: float


class AiUsageServiceOut(CamelModel):
    service: str  # generation | colearner | kg
    requests: int
    cost_inr: float
    cost_usd: float
    errors: int


class AiUsageTeacherOut(CamelModel):
    teacher_id: str
    name: str | None
    school_name: str | None
    requests: int
    cost_inr: float


class AiUsageWastedOut(CamelModel):
    """Failed requests that still cost something (a model ran, the result was lost)."""
    requests: int
    cost_inr: float


class AiUsageBudgetOut(CamelModel):
    monthly_inr: float | None  # AI_MONTHLY_BUDGET_INR; None = no budget set
    spent_this_month_inr: float  # platform-wide, current IST month
    projected_month_inr: float


class AiUsageSummaryOut(CamelModel):
    range: AiUsageRangeOut
    totals: AiUsageTotalsOut
    by_day: list[AiUsageDayOut]
    by_feature: list[AiUsageFeatureOut]
    by_model: list[AiUsageModelOut]
    by_school: list[AiUsageSchoolOut]
    top_students: list[AiUsageStudentOut]
    budget: AiUsageBudgetOut
    by_service: list[AiUsageServiceOut] = []
    top_teachers: list[AiUsageTeacherOut] = []
    wasted: AiUsageWastedOut | None = None


class AiUsageEventOut(CamelModel):
    id: str
    created_at: datetime
    service: str
    endpoint: str
    feature: str
    model: str | None
    school_name: str | None
    student_name: str | None
    teacher_name: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_inr: float
    latency_ms: int | None
    status: int
    error: str | None
    models: list[str] = []
    cached_input_tokens: int = 0
    request_id: str | None = None  # find this request in the services' logs
    calls: int = 1


class AiUsageEventsOut(CamelModel):
    items: list[AiUsageEventOut]
    next_cursor: str | None


class AiUsageSchoolOptionOut(CamelModel):
    id: str
    name: str


class AiUsageFiltersOut(CamelModel):
    features: list[str]
    models: list[str]
    services: list[str] = []
    schools: list[AiUsageSchoolOptionOut]
