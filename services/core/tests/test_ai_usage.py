"""Every AI request Core makes is persisted to ai_usage_events with its cost
(src/services/ai_usage.py): from generation/colearner's X-Nool-Cost header,
or a live voice test's finish payload - labelled with ai_context's feature and
ids, and never able to break the user's request."""

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from src.api.routes import ai_assessor
from src.api.schemas.ai_assessor import FinishSessionIn
from src.domain.models import AiUsageEvent, Role
from src.services import ai_context, ai_usage, colearner_client, generation_client, paper_copilot_client

_HEADER = {"models": ["gpt-5.1", "gpt-5-mini"], "calls": 3, "input_tokens": 1200, "cached_input_tokens": 200,
           "output_tokens": 300, "usd": 0.0042}


def _user(role: Role):
    return SimpleNamespace(id=uuid.uuid4(), role=role, school_id=uuid.uuid4())


@pytest.fixture
def rows(monkeypatch) -> list[AiUsageEvent]:
    """Captures what record() writes, instead of a real DB."""
    added: list[AiUsageEvent] = []

    class _Session:
        def add(self, row):
            added.append(row)

        async def commit(self):
            pass

    @asynccontextmanager
    async def fake_session():
        yield _Session()

    monkeypatch.setattr(ai_usage, "get_session", fake_session)
    return added


_REAL_CLIENT = httpx.AsyncClient


def _stub(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("GENERATION_SERVICE_URL", "http://generation-test")
    monkeypatch.setenv("COLEARNER_SERVICE_URL", "http://colearner-test")


def _ok(body: dict, cost: dict | None = None):
    headers = {"X-Nool-Cost": json.dumps(cost)} if cost else {}
    return lambda request: httpx.Response(200, json=body, headers=headers)


# ---- record() ---------------------------------------------------------------


async def test_record_builds_the_row_from_the_header_and_the_callers_context(rows):
    student = _user(Role.STUDENT)
    ai_context.set_caller(student)
    response = httpx.Response(200, headers={"X-Nool-Cost": json.dumps(_HEADER)})
    with ai_context.feature("daily_practice_mcq"):
        await ai_usage.record("generation", "/v1/homework/gap-fill", "mcq_check", started=time.perf_counter(), response=response)

    [row] = rows
    assert (row.service, row.endpoint, row.feature) == ("generation", "/v1/homework/gap-fill", "daily_practice_mcq")
    assert row.model == "gpt-5.1" and row.models == ["gpt-5.1", "gpt-5-mini"] and row.calls == 3
    assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (1200, 200, 300)
    assert row.usd == 0.0042 and row.inr == round(0.0042 * 86.5, 4)  # no "inr" in the header
    assert row.status == 200 and row.error is None and row.latency_ms >= 0
    assert (row.student_id, row.school_id, row.teacher_id) == (student.id, student.school_id, None)


async def test_record_takes_the_headers_inr_and_a_network_error_is_status_0(rows):
    ai_context.set_caller(_user(Role.TEACHER))
    await ai_usage.record("colearner", "/x", "assistant", cost={"usd": 1, "inr": 90, "calls": 1})
    await ai_usage.record("colearner", "/x", "assistant", started=time.perf_counter(), error=httpx.ConnectError("refused"))
    assert rows[0].inr == 90 and rows[0].latency_ms is None and rows[0].status == 200
    assert (rows[1].status, rows[1].error, rows[1].calls, rows[1].usd) == (0, "ConnectError: refused", 0, 0)


async def test_record_never_raises_when_the_db_fails(monkeypatch, caplog):
    @asynccontextmanager
    async def broken():
        raise RuntimeError("db down")
        yield

    monkeypatch.setattr(ai_usage, "get_session", broken)
    with caplog.at_level(logging.WARNING):
        await ai_usage.record("generation", "/v1/x", "homework", started=time.perf_counter(), response=httpx.Response(200))
        await ai_usage.record("generation", "/v1/x", "homework",
                              response=httpx.Response(200, headers={"X-Nool-Cost": "not json"}))
    assert any(r.getMessage() == "ai_usage_record_failed" for r in caplog.records)


async def test_record_writes_a_real_row(db_session, monkeypatch):
    @asynccontextmanager
    async def test_session():
        yield db_session

    monkeypatch.setattr(ai_usage, "get_session", test_session)
    student = _user(Role.STUDENT)
    ai_context.set_caller(student)
    await ai_usage.record("generation", "/v1/mcq/explain", "mcq_explain", started=time.perf_counter(),
                          response=httpx.Response(200, headers={"X-Nool-Cost": json.dumps(_HEADER)}))
    row = (await db_session.execute(
        AiUsageEvent.__table__.select().where(AiUsageEvent.student_id == student.id))).one()
    assert row.feature == "mcq_explain" and row.usd == Decimal("0.004200") and row.models == _HEADER["models"]
    assert row.created_at is not None


# ---- client hooks ----------------------------------------------------------


async def test_generation_calls_are_recorded_on_success_and_failure(monkeypatch, rows):
    ai_context.set_caller(_user(Role.STUDENT))
    _stub(monkeypatch, _ok({"results": []}, _HEADER))
    await generation_client.explain_mcq({})
    await paper_copilot_client.plan({})

    _stub(monkeypatch, lambda request: httpx.Response(
        503, json={"error": {"code": "HTTP_ERROR", "message": "x"}}, headers={"X-Nool-Cost": json.dumps(_HEADER)}))
    with pytest.raises(generation_client.GenerationServiceUnavailableError):
        await generation_client.gap_fill({})

    def unreachable(request):
        raise httpx.ConnectError("no route")

    _stub(monkeypatch, unreachable)
    with pytest.raises(generation_client.GenerationServiceUnavailableError):
        await generation_client.transcribe("", "audio/m4a", "")

    assert [(r.endpoint, r.feature, r.status) for r in rows] == [
        ("/v1/mcq/explain", "mcq_explain", 200), ("/v1/paper/plan", "paper", 200),
        ("/v1/homework/gap-fill", "homework", 503), ("/v1/transcribe", "practice_transcribe", 0)]
    assert rows[2].error == "HTTP 503 HTTP_ERROR" and rows[2].calls == 3  # a failed request can still cost
    assert rows[3].error == "ConnectError: no route" and rows[3].latency_ms is not None


async def test_colearner_grading_is_recorded_and_a_live_call_only_on_failure(monkeypatch, rows):
    ai_context.set_caller(_user(Role.STUDENT))
    _stub(monkeypatch, _ok({"score": 80, "sdp": "a", "call_id": "c"}, {**_HEADER, "inr": 0.5}))
    await colearner_client.grade_answer(question="q", expected_points=[], misconceptions=[], transcript="t",
                                        subject=None, token="tok")
    await colearner_client.regrade_transcript(config={}, transcript=[], answers=[], token="tok")
    await colearner_client.create_realtime_call(sdp="o", config={}, owner_ref="s", token="tok")  # not recorded
    _stub(monkeypatch, lambda request: httpx.Response(503, json={}))
    with pytest.raises(colearner_client.ColearnerUnavailableError):
        await colearner_client.create_realtime_call(sdp="o", config={}, owner_ref="s", token="tok")

    assert [(r.service, r.feature, r.status) for r in rows] == [
        ("colearner", "practice_grade", 200), ("colearner", "voice_regrade", 200), ("colearner", "voice_test", 503)]
    assert rows[0].inr == 0.5 and rows[0].endpoint == "/api/v1/colearner/grade-answer"


async def test_finish_session_records_the_live_calls_cost(monkeypatch, rows):
    student = _user(Role.STUDENT)
    ai_context.set_caller(student)
    ai_session = SimpleNamespace(id=uuid.uuid4(), test_id=None, created_at=datetime.now(timezone.utc))
    finish = {"answered": 2, "answers": [], "model_completed": True, "cost": {
        "total_usd": 0.05, "total_inr": 4.325, "duration_seconds": 300.0,
        "tokens": {"text_in": 1000, "text_in_cached": 4000, "audio_in": 2000, "audio_in_cached": 500,
                   "text_out": 300, "audio_out": 1200, "transcribe_in": 100, "transcribe_out": 50},
        "models": {"live_call": "gpt-realtime-2.1-mini", "transcription": "gpt-4o-mini-transcribe"}}}

    async def owned(db, session_id, user):
        return ai_session, []

    async def fake_finish(**kwargs):
        return finish

    async def nothing(*args, **kwargs):
        return []

    monkeypatch.setattr(ai_assessor, "_owned_session", owned)
    monkeypatch.setattr(ai_assessor, "_test_questions", nothing)
    monkeypatch.setattr(ai_assessor, "finish_realtime_call", fake_finish)
    monkeypatch.setattr(ai_assessor, "_record_completion", nothing)
    out = await ai_assessor.finish_session(str(ai_session.id), FinishSessionIn(call_id="call-1"),
                                           authorization="Bearer t", app_user=student, db=None)
    assert out.outcome == "completed"
    [row] = rows
    assert (row.service, row.feature, row.model) == ("colearner", "voice_test", "gpt-realtime-2.1-mini")
    assert row.models == ["gpt-realtime-2.1-mini", "gpt-4o-mini-transcribe"]
    assert (row.usd, row.inr, row.latency_ms, row.status) == (0.05, 4.325, None, 200)
    assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (7600, 4500, 1550)
    assert row.student_id == student.id


def test_realtime_cost_tolerates_a_missing_or_odd_payload():
    assert ai_usage.realtime_cost(None) == {}
    assert ai_usage.realtime_cost({"total_usd": 0.01})["usd"] == 0.01

