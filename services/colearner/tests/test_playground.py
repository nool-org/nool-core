def test_playground_off_by_default(client):
    assert client.get("/api/v1/colearner/playground").status_code == 404
    assert client.post("/api/v1/colearner/realtime/calls/x/finish", json={"owner_ref": "x"}).status_code == 401


def test_playground_on_serves_page_and_skips_auth(client, monkeypatch):
    monkeypatch.setenv("PLAYGROUND_ENABLED", "true")
    res = client.get("/api/v1/colearner/playground")
    assert res.status_code == 200 and "Co-Learner Playground" in res.text
    # No header -> let through (404 = unknown call, i.e. past the auth check).
    assert client.post("/api/v1/colearner/realtime/calls/x/finish", json={"owner_ref": "x"}).status_code == 404


def test_playground_on_still_verifies_a_sent_token(client, monkeypatch):
    from shared.errors import UnauthorizedError

    def fake_verify(token: str):
        raise UnauthorizedError("Invalid or expired authentication token.")

    monkeypatch.setenv("PLAYGROUND_ENABLED", "true")
    monkeypatch.setattr("src.api.deps.verify_token", fake_verify)
    res = client.post("/api/v1/colearner/realtime/calls/x/finish", json={"owner_ref": "x"}, headers={"Authorization": "Bearer bad"})
    assert res.status_code == 401
