def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_echoes_request_id(client):
    response = client.get("/health", headers={"X-Request-ID": "test-request-id"})

    assert response.headers["X-Request-ID"] == "test-request-id"


def test_database_data_error_is_a_422_not_a_500(client):
    # V-014/V-019: a non-UUID id or an out-of-range number reaching the database
    import asyncpg
    from sqlalchemy.exc import DBAPIError

    @client.app.get("/_data_error")
    async def boom():
        orig = Exception("adapter")
        orig.__cause__ = asyncpg.DataError("invalid input for query argument $1: 'x' (invalid UUID)")
        raise DBAPIError("select", {}, orig)

    response = client.get("/_data_error")
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
