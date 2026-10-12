import asyncio
import json
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.main import create_app
from src.services import cost
from src.services.llm import OpenAIStructured
from src.services.teacher_assistant import SummaryResult

APP = create_app()  # at import: create_app resets the root log handlers, which would drop caplog's


def test_usd_bills_cached_and_audio_input_at_their_own_rates():
    # 1M in (200k cached) + 100k out on gpt-5.1: 0.8*1.25 + 0.2*0.125 + 0.1*10
    assert cost.usd("gpt-5.1", input_tokens=1_000_000, cached_input_tokens=200_000, output_tokens=100_000) == 2.025
    # transcribe: 1000 audio + 100 text in, 50 out
    assert cost.usd("gpt-4o-mini-transcribe", input_tokens=1100, audio_input_tokens=1000,
                    output_tokens=50) == round((1000 * 3.0 + 100 * 1.25 + 50 * 5.0) / 1e6, 7)
    assert cost.usd("gpt-4o-mini-transcribe", seconds=120) == 0.006
    assert cost.usd("some-new-model", input_tokens=10) is None


class _FakeResponses:
    async def parse(self, **kwargs):
        usage = SimpleNamespace(input_tokens=2000, input_tokens_details=SimpleNamespace(cached_tokens=1000),
                                output_tokens=300, output_tokens_details=SimpleNamespace(reasoning_tokens=100))
        return SimpleNamespace(output_parsed=SummaryResult(bullets=["ok"]), status="completed", usage=usage)


def test_each_llm_call_logs_its_cost_with_cores_caller_headers(caplog):
    llm = OpenAIStructured("unused")
    llm.client = SimpleNamespace(responses=_FakeResponses())
    APP.state.llm = llm
    headers = {"X-Nool-Feature": "summary", "X-Nool-Teacher": "t-1", "X-Nool-School": "s-1"}
    with caplog.at_level(logging.INFO):
        r = TestClient(APP).post("/v1/teacher-assistant/summary", json={"facts": {}}, headers=headers)
    assert r.status_code == 200
    [line] = [rec.cost for rec in caplog.records if rec.getMessage() == "generation_call_cost"]
    assert line["model"] == "gpt-5-mini" and line["feature"] == "summary"
    assert line["teacher"] == "t-1" and line["school"] == "s-1" and "student" not in line
    assert line["endpoint"] == "/v1/teacher-assistant/summary"
    assert (line["input_tokens"], line["cached_input_tokens"], line["output_tokens"], line["reasoning_tokens"]) == (
        2000, 1000, 300, 100)
    assert line["usd"] == round((1000 * 0.25 + 1000 * 0.025 + 300 * 2.0) / 1e6, 7)
    assert line["inr"] == round(line["usd"] * cost.USD_TO_INR_RATE, 5)


def _usage(input_tokens, cached, output):
    return SimpleNamespace(input_tokens=input_tokens, input_tokens_details=SimpleNamespace(cached_tokens=cached),
                           output_tokens=output, output_tokens_details=SimpleNamespace(reasoning_tokens=0))


def test_the_requests_calls_are_summed_into_the_x_nool_cost_header():
    """Two calls (one of them in a child task, like gap-fill's gather) -> one header with the sum."""
    async def two_calls(*, model, output_type, **_):
        cost.log_response_usage("gpt-5.1", _usage(1000, 200, 100))

        async def child():  # gather runs it as its own task, with a copy of the context
            cost.log_response_usage("gpt-5-mini", _usage(500, 0, 50))

        await asyncio.gather(child())
        return output_type(bullets=["ok"])

    APP.state.llm = two_calls
    r = TestClient(APP).post("/v1/teacher-assistant/summary", json={"facts": {}})
    assert r.status_code == 200
    header = json.loads(r.headers["x-nool-cost"])
    expected_usd = round(cost.usd("gpt-5.1", input_tokens=1000, cached_input_tokens=200, output_tokens=100)
                         + cost.usd("gpt-5-mini", input_tokens=500, output_tokens=50), 7)
    assert header == {"models": ["gpt-5.1", "gpt-5-mini"], "calls": 2, "input_tokens": 1500,
                      "cached_input_tokens": 200, "output_tokens": 150, "usd": expected_usd}
    assert " " not in r.headers["x-nool-cost"]  # compact


def test_a_failed_request_that_paid_for_a_call_still_returns_its_cost():
    async def paid_then_broke(*, model, **_):
        cost.log_response_usage("gpt-5-mini", _usage(100, 0, 10))
        raise RuntimeError("unparsable")

    APP.state.llm = paid_then_broke
    r = TestClient(APP, raise_server_exceptions=False).post("/v1/teacher-assistant/summary", json={"facts": {}})
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert json.loads(r.headers["x-nool-cost"])["calls"] == 1


def test_no_model_call_means_no_cost_header():
    APP.state.llm = None
    r = TestClient(APP).post("/v1/teacher-assistant/summary", json={"facts": {}})
    assert r.status_code == 503 and "x-nool-cost" not in r.headers
