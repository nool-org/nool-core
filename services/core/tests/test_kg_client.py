"""kg_client._get must tell "nothing found" (a legitimate 404 from the kg
service) apart from "couldn't reach the service" - conflating the two used
to turn a real "no curriculum for this board/grade/subject" response into a
generic 503 KgServiceUnavailableError, confirmed live via the Curriculum
page's per-Subject sync hitting a subject name nool-kg doesn't have.
"""

import httpx
import pytest

from src.services import kg_client


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("GET", "http://kg-test"), response=self)

    def json(self) -> dict:
        return self._json or {}


@pytest.mark.asyncio
async def test_get_curriculum_tree_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(kg_client, "_base_url", lambda: "http://kg-test")

    async def fake_get(self, path, params=None):
        return _FakeResponse(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await kg_client.get_curriculum_tree(board="CBSE", grade=10, subject="Nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_curriculum_tree_returns_the_tree_on_success(monkeypatch):
    monkeypatch.setattr(kg_client, "_base_url", lambda: "http://kg-test")

    async def fake_get(self, path, params=None):
        return _FakeResponse(200, {"subject": "Science", "chapters": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await kg_client.get_curriculum_tree(board="CBSE", grade=10, subject="Science")
    assert result == {"subject": "Science", "chapters": []}


@pytest.mark.asyncio
async def test_get_curriculum_tree_raises_service_unavailable_on_connection_error(monkeypatch):
    monkeypatch.setattr(kg_client, "_base_url", lambda: "http://kg-test")

    async def fake_get(self, path, params=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(kg_client.KgServiceUnavailableError):
        await kg_client.get_curriculum_tree(board="CBSE", grade=10, subject="Science")


@pytest.mark.asyncio
async def test_generate_assessor_content_returns_context_and_no_reference_questions(monkeypatch):
    monkeypatch.setattr(kg_client, "_base_url", lambda: "http://kg-test")

    async def fake_post(self, path, json=None, headers=None):
        assert path == "/voice-tests/generate"
        return _FakeResponse(200, {"textbook_context": "Rusting is...", "reference_questions": ["stale"]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await kg_client.generate_assessor_content(
        subject="Science", board="CBSE", grade=10, chapters=[{"number": 1, "name": "Ch 1"}],
        topic_names=[], bloom_levels=["APPLY"], num_questions=5,
    )
    assert result == {"textbook_context": "Rusting is...", "reference_questions": []}
