def test_ready_is_not_ready_without_firebase_project(client, monkeypatch):
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert {c["name"]: c["ok"] for c in body["checks"]} == {"firebase_config": False, "model_keys": True}


def test_ready_is_not_ready_without_any_model_key(client, monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-project")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert {c["name"]: c["ok"] for c in body["checks"]} == {"firebase_config": True, "model_keys": False}


def test_ready_is_ready_when_firebase_and_a_model_key_configured(client, monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-project")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
