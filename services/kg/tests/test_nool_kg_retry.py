"""nool_kg_client retry / error handling against an httpx.MockTransport."""

import httpx
import pytest
import pytest_asyncio

from src.services import nool_kg_client


@pytest_asyncio.fixture
async def mock_kg(monkeypatch):
    """Yields setup({path: [responses or exceptions]}) -> request log. Each
    request pops the next item for its path; the last one repeats."""
    async def no_sleep(_):
        pass
    monkeypatch.setattr(nool_kg_client.asyncio, "sleep", no_sleep)
    routes: dict[str, list] = {}
    log: list[httpx.Request] = []

    def handler(request: httpx.Request):
        log.append(request)
        queue = routes[request.url.path]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result

    await nool_kg_client.dispose_client()
    nool_kg_client._client = httpx.AsyncClient(base_url="http://kg", transport=httpx.MockTransport(handler))

    def setup(path_responses):
        routes.update(path_responses)
        return log
    yield setup
    await nool_kg_client.dispose_client()


@pytest.mark.asyncio
async def test_retries_5xx_then_succeeds(mock_kg):
    log = mock_kg({"/api/subjects": [httpx.Response(502), httpx.Response(503), httpx.Response(200, json=["G"])]})
    assert await nool_kg_client.list_subjects() == ["G"]
    assert len(log) == 3


@pytest.mark.asyncio
async def test_persistent_5xx_gives_up_after_two_retries(mock_kg):
    log = mock_kg({"/api/subjects": [httpx.Response(500)]})
    with pytest.raises(httpx.HTTPStatusError):
        await nool_kg_client.list_subjects()
    assert len(log) == 3


@pytest.mark.asyncio
async def test_retries_timeouts_then_raises(mock_kg):
    log = mock_kg({"/api/subjects": [httpx.ReadTimeout("slow")]})
    with pytest.raises(httpx.ReadTimeout):
        await nool_kg_client.list_subjects()
    assert len(log) == 3  # 1 try + 2 retries


@pytest.mark.asyncio
async def test_timeout_then_success(mock_kg):
    log = mock_kg({"/api/subjects": [httpx.ConnectTimeout("slow"), httpx.Response(200, json=[])]})
    assert await nool_kg_client.list_subjects() == []
    assert len(log) == 2


@pytest.mark.asyncio
async def test_4xx_is_not_retried(mock_kg):
    log = mock_kg({"/api/subjects": [httpx.Response(404)]})
    with pytest.raises(httpx.HTTPStatusError):
        await nool_kg_client.list_subjects()
    assert len(log) == 1


@pytest.mark.asyncio
async def test_non_200_sub_responses_are_logged_not_silent(mock_kg, caplog):
    mock_kg({
        "/api/subjects": [httpx.Response(200, json=["Science_Grade_10_NCERT"])],
        "/api/subjects/Science_Grade_10_NCERT/chapters": [httpx.Response(404)],
    })
    assert await nool_kg_client.list_curriculum_subjects() == []
    assert any("nool_kg_unexpected_status" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,healthy", [(200, True), (204, True), (401, False), (404, False), (503, False)])
async def test_check_connection_only_2xx_is_healthy(mock_kg, status, healthy):
    log = mock_kg({"/": [httpx.Response(status)]})
    assert await nool_kg_client.check_connection() is healthy
    assert len(log) == 1  # /ready never retries


@pytest.mark.asyncio
async def test_init_client_sends_api_key_header_only_when_configured():
    await nool_kg_client.dispose_client()
    assert "X-API-Key" not in nool_kg_client.init_client("http://kg").headers
    await nool_kg_client.dispose_client()
    assert nool_kg_client.init_client("http://kg", api_key="k").headers["X-API-Key"] == "k"
    await nool_kg_client.dispose_client()
