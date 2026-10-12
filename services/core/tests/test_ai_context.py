"""Core tags every generation/colearner call with X-Nool-* headers (feature +
the requesting student/teacher/school) so their cost logs can be summed per
student - see src/services/ai_context.py and scripts/cost_report.py."""

import uuid
from types import SimpleNamespace

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.domain.models import Role
from src.services import ai_context, colearner_client, generation_client


def _stub_transport(monkeypatch, response: dict) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("GENERATION_SERVICE_URL", "http://generation-test")
    monkeypatch.setenv("COLEARNER_SERVICE_URL", "http://colearner-test")
    return seen


def _user(role: Role):
    return SimpleNamespace(id=uuid.uuid4(), role=role, school_id=uuid.uuid4())


async def test_generation_calls_carry_the_feature_and_the_students_ids(monkeypatch):
    seen = _stub_transport(monkeypatch, {"questions": [], "results": []})
    student = _user(Role.STUDENT)
    ai_context.set_caller(student)

    await generation_client.generate_mcq({})
    with ai_context.feature("daily_practice_mcq"):
        await generation_client.explain_mcq({})
    await generation_client.explain_mcq({})  # the override ends with its block

    assert [r.headers["x-nool-feature"] for r in seen] == ["mcq_check", "daily_practice_mcq", "mcq_explain"]
    assert seen[0].headers["x-nool-student"] == str(student.id)
    assert seen[0].headers["x-nool-school"] == str(student.school_id)
    assert "x-nool-teacher" not in seen[0].headers


async def test_colearner_grade_answer_carries_the_feature_and_keeps_the_token(monkeypatch):
    seen = _stub_transport(monkeypatch, {"score": 80})
    ai_context.set_caller(_user(Role.STUDENT))
    await colearner_client.grade_answer(question="q", expected_points=[], misconceptions=[], transcript="t",
                                        subject=None, token="tok")
    assert seen[0].headers["x-nool-feature"] == "practice_grade" and seen[0].headers["authorization"] == "Bearer tok"


def test_the_caller_set_by_an_auth_dependency_reaches_the_endpoints_ai_calls(monkeypatch):
    """get_current_app_user is an async dependency: what it sets must still be
    visible when the endpoint later calls generation."""
    seen = _stub_transport(monkeypatch, {"bullets": []})
    teacher = _user(Role.TEACHER)

    async def current_user():
        ai_context.set_caller(teacher)
        return teacher

    app = FastAPI()

    @app.get("/x")
    async def endpoint(_=Depends(current_user)):
        return await generation_client.teacher_summary({})

    assert TestClient(app).get("/x").status_code == 200
    assert seen[0].headers["x-nool-teacher"] == str(teacher.id)
    assert seen[0].headers["x-nool-feature"] == "summary"
