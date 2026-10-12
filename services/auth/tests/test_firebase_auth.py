"""Direct tests of FirebaseIdentityProvider's role extraction and
per-exception-type error handling - lives in services/auth/tests since
there is no shared-level test runner configured (see services/auth/tests
elsewhere for the same pattern testing shared code through a consumer).
"""

import httpx
import pytest
from firebase_admin import auth as firebase_auth

from shared.auth.providers.firebase import FirebaseIdentityProvider, FirebaseNotConfiguredError
from shared.errors import UnauthorizedError


def _provider_with_bypassed_init(web_api_key: str | None = None) -> FirebaseIdentityProvider:
    """verify_token() calls _get_app() first, which would otherwise try to
    actually initialize a Firebase app. Setting the cached _app directly
    skips that - these tests only care about what happens after.
    """
    provider = FirebaseIdentityProvider(
        project_id="test-project", credentials_path=None, web_api_key=web_api_key
    )
    provider._app = object()  # noqa: SLF001 - test-only bypass, see docstring
    return provider


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def test_verify_token_extracts_role_claim(monkeypatch):
    provider = _provider_with_bypassed_init()
    monkeypatch.setattr(
        firebase_auth,
        "verify_id_token",
        lambda token, app: {"uid": "u1", "email": "t@example.com", "role": "TEACHER"},
    )

    user = provider.verify_token("token")

    assert user.role == "TEACHER"
    assert user.claims["role"] == "TEACHER"


def test_verify_token_role_is_none_when_claim_absent(monkeypatch):
    provider = _provider_with_bypassed_init()
    monkeypatch.setattr(
        firebase_auth, "verify_id_token", lambda token, app: {"uid": "u1", "email": "t@example.com"}
    )

    user = provider.verify_token("token")

    assert user.role is None


@pytest.mark.parametrize(
    "raise_error",
    [
        lambda: firebase_auth.ExpiredIdTokenError("expired", cause=None),
        lambda: firebase_auth.RevokedIdTokenError("revoked"),
        lambda: firebase_auth.UserDisabledError("disabled"),
        lambda: firebase_auth.InvalidIdTokenError("invalid"),
        lambda: RuntimeError("unexpected transport failure"),
    ],
)
def test_verify_token_maps_every_failure_to_unauthorized(monkeypatch, raise_error):
    provider = _provider_with_bypassed_init()

    def fake_verify(token, app):
        raise raise_error()

    monkeypatch.setattr(firebase_auth, "verify_id_token", fake_verify)

    with pytest.raises(UnauthorizedError):
        provider.verify_token("token")


def test_authenticate_with_password_returns_custom_token_on_success(monkeypatch):
    provider = _provider_with_bypassed_init(web_api_key="test-key")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(200, {"localId": "uid-123"})
    )
    monkeypatch.setattr(
        firebase_auth, "create_custom_token", lambda uid, app: uid.encode("utf-8")
    )

    result = provider.authenticate_with_password("a@example.com", "correct-password")

    assert result.custom_token == "uid-123"


def test_authenticate_with_password_rejects_wrong_password(monkeypatch):
    provider = _provider_with_bypassed_init(web_api_key="test-key")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}),
    )

    with pytest.raises(UnauthorizedError):
        provider.authenticate_with_password("a@example.com", "wrong-password")


def test_authenticate_with_password_rejects_nonexistent_email_the_same_way(monkeypatch):
    """Firebase's own API collapses "no such account" into the same
    generic error as a wrong password (enumeration protection) - this
    provider must not try to recover or expose that distinction either.
    """
    provider = _provider_with_bypassed_init(web_api_key="test-key")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}),
    )

    with pytest.raises(UnauthorizedError, match="Incorrect email or password."):
        provider.authenticate_with_password("nobody@example.com", "anything")


def test_authenticate_with_password_requires_web_api_key(monkeypatch):
    provider = _provider_with_bypassed_init(web_api_key=None)

    with pytest.raises(FirebaseNotConfiguredError):
        provider.authenticate_with_password("a@example.com", "correct-password")


def test_authenticate_with_password_maps_network_failure_to_upstream_error(monkeypatch):
    from shared.auth.providers.firebase import IdentityProviderUnavailableError

    provider = _provider_with_bypassed_init(web_api_key="test-key")

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(IdentityProviderUnavailableError):
        provider.authenticate_with_password("a@example.com", "correct-password")


def test_get_app_reuses_the_default_app_another_provider_already_started(monkeypatch):
    import firebase_admin

    existing = object()
    monkeypatch.setattr(firebase_admin, "get_app", lambda *a: existing)

    def must_not_init(*a, **k):
        raise AssertionError("initialize_app must not run twice in one process")

    monkeypatch.setattr(firebase_admin, "initialize_app", must_not_init)
    provider = FirebaseIdentityProvider(project_id="test-project", credentials_path=None)
    assert provider._get_app() is existing  # noqa: SLF001
