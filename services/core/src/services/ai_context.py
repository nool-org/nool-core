"""Who an AI call is for - sent to generation and colearner as X-Nool-*
headers so their cost log lines (generation_call_cost, grade_answer_cost,
realtime_call_started) can be summed per feature and per student by
scripts/cost_report.py. Context only: never used for authorization."""

from contextlib import contextmanager
from contextvars import ContextVar

_caller: ContextVar[dict[str, str]] = ContextVar("ai_caller", default={})
_feature: ContextVar[str | None] = ContextVar("ai_feature", default=None)


def set_caller(user) -> None:
    """The request's user as Student/Teacher + School ids - set once by get_current_app_user."""
    role = getattr(user.role, "value", user.role)
    ids = {"Student": user.id if role == "STUDENT" else None, "Teacher": user.id if role == "TEACHER" else None,
           "School": user.school_id}
    _caller.set({k: str(v) for k, v in ids.items() if v})


@contextmanager
def feature(name: str):
    """Labels the AI calls inside the block, overriding the client's default
    (e.g. generate_mcq is "mcq_check" unless daily practice says otherwise)."""
    token = _feature.set(name)
    try:
        yield
    finally:
        _feature.reset(token)


def headers(default_feature: str) -> dict[str, str]:
    return {f"X-Nool-{k}": v for k, v in {**_caller.get(), "Feature": _feature.get() or default_feature}.items()}
