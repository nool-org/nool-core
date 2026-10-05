"""Auth-boundary tests, following the same pattern as
services/core/tests/test_deps.py: "no header" is tested against the real
dependency chain (UnauthorizedError raises before any Firebase call), and
"invalid token" monkeypatches src.api.deps.verify_token directly rather
than exercising a real Firebase network call.
"""

import pytest
from shared.errors import UnauthorizedError


@pytest.mark.parametrize("path", ["/api/v1/colearner/cascaded"])
def test_post_requires_authorization_header(client, path):
    response = client.post(path, json={})

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/colearner/cascaded"])
def test_post_rejects_invalid_bearer_token(client, path, monkeypatch):
    def fake_verify(token: str):
        raise UnauthorizedError("Invalid or expired authentication token.")

    monkeypatch.setattr("src.api.deps.verify_token", fake_verify)

    response = client.post(path, json={}, headers={"Authorization": "Bearer bad-token"})

    assert response.status_code == 401
