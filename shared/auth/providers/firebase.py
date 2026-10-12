import threading

import firebase_admin
import httpx
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

from ...errors.exceptions import AppError, UnauthorizedError
from ...logging import get_logger
from ..provider import AuthenticatedUser, PasswordAuthResult

logger = get_logger(__name__)

# Firebase's own REST endpoint for a password credential check - there is
# no Admin SDK call that does this (the Admin SDK can mint/verify tokens
# and manage accounts, but deliberately cannot check a password itself).
# This is the same public Identity Toolkit API a Firebase client SDK
# calls under the hood for signInWithEmailAndPassword; calling it
# server-side (instead of from the browser/app, as this codebase used to)
# is exactly the change authenticate_with_password exists to make.
_SIGN_IN_WITH_PASSWORD_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"

# Audit-trail event names (see the `event` extra field in
# shared/logging/formatter.py). Every token verification outcome - success
# or failure - emits exactly one of these, so "who was authenticated, when,
# and how many failures preceded it" is answerable by filtering logs on
# `event`, without a database table (see root CLAUDE.md's "Current Goal" -
# no product schema yet, so an audit table isn't in scope either).
_EVENT_TOKEN_VERIFIED = "auth.token_verified"
_EVENT_TOKEN_VERIFICATION_FAILED = "auth.token_verification_failed"
_EVENT_PASSWORD_AUTH_SUCCEEDED = "auth.password_auth_succeeded"
_EVENT_PASSWORD_AUTH_FAILED = "auth.password_auth_failed"

# Token checks run in worker threads, so a burst of requests right after a
# (re)start would all try to start the process-wide default app at once -
# all but one failing with "default app already exists" (500 -> the app
# signs the user out). One at a time; the rest reuse the started app.
_init_lock = threading.Lock()


class FirebaseNotConfiguredError(AppError):
    code = "AUTH_NOT_CONFIGURED"
    status_code = 500


class IdentityProviderUnavailableError(AppError):
    """The identity provider itself couldn't be reached (network/DNS/
    timeout) - distinct from bad credentials, which is UnauthorizedError.
    """

    code = "UPSTREAM_ERROR"
    status_code = 502


class FirebaseIdentityProvider:
    """Firebase Authentication, implementing the IdentityProvider Protocol
    (see ../provider.py). This is the only file in the codebase that
    should import firebase_admin - route handlers, business logic, and
    services/*/src/api/deps.py only ever see AuthenticatedUser and
    UnauthorizedError, never a Firebase type.

    Each service's deps.py constructs one of these from its own settings
    and passes it to configure_identity_provider() - see
    services/auth/src/services/token_service.py and
    services/core/src/api/deps.py for the two (currently identical) call
    sites. That construction call is the *only* place in the codebase
    that names "Firebase" concretely; swapping providers means writing a
    sibling class here (e.g. KeycloakIdentityProvider) and changing those
    two call sites - nothing else.
    """

    def __init__(
        self,
        project_id: str | None,
        credentials_path: str | None,
        web_api_key: str | None = None,
    ) -> None:
        self._project_id = project_id
        self._credentials_path = credentials_path
        # Only needed by authenticate_with_password (i.e. only the Auth
        # service's construction call passes this) - token verification
        # never needs it, so it stays optional here rather than forcing
        # every caller (e.g. Core, which only verifies tokens) to supply
        # a value it has no use for.
        self._web_api_key = web_api_key
        self._app: firebase_admin.App | None = None

    def _get_app(self) -> firebase_admin.App:
        if self._app is not None:
            return self._app
        if not self._project_id:
            raise FirebaseNotConfiguredError("Firebase project is not configured.")
        with _init_lock:
            return self._start_app()

    def _start_app(self) -> firebase_admin.App:
        if self._app is not None:
            return self._app
        try:
            # The default app is process-wide: if another provider instance in this process (e.g. a
            # dev auto-reload building the app twice) already started it, reuse it instead of failing.
            self._app = firebase_admin.get_app()
            return self._app
        except ValueError:
            pass
        try:
            options = {"projectId": self._project_id}
            if self._credentials_path:
                cred = credentials.Certificate(self._credentials_path)
                self._app = firebase_admin.initialize_app(cred, options)
            else:
                self._app = firebase_admin.initialize_app(options=options)
        except Exception as exc:
            logger.error("firebase_init_failed", extra={"error": str(exc)})
            raise FirebaseNotConfiguredError("Failed to initialize Firebase Admin SDK.") from exc
        return self._app

    def verify_token(self, token: str) -> AuthenticatedUser:
        app = self._get_app()
        try:
            decoded = firebase_auth.verify_id_token(token, app=app)
        except firebase_auth.ExpiredIdTokenError as exc:
            self._log_failure("expired")
            raise UnauthorizedError("Invalid or expired authentication token.") from exc
        except firebase_auth.RevokedIdTokenError as exc:
            self._log_failure("revoked")
            raise UnauthorizedError("Invalid or expired authentication token.") from exc
        except firebase_auth.UserDisabledError as exc:
            self._log_failure("user_disabled")
            raise UnauthorizedError("Invalid or expired authentication token.") from exc
        except firebase_auth.InvalidIdTokenError as exc:
            self._log_failure("invalid")
            raise UnauthorizedError("Invalid or expired authentication token.") from exc
        except Exception as exc:
            # Anything else (e.g. transient network/certificate-fetch
            # failure) - same client-facing message, but logged distinctly
            # (both event=... and a WARNING level, not INFO) since these
            # are infra issues worth noticing rather than routine bad
            # tokens.
            logger.warning(
                "token_verification_error",
                extra={"event": _EVENT_TOKEN_VERIFICATION_FAILED, "reason": "error", "error": str(exc)},
            )
            raise UnauthorizedError("Invalid or expired authentication token.") from exc

        logger.info(
            "token_verified",
            extra={"event": _EVENT_TOKEN_VERIFIED, "uid": decoded["uid"]},
        )
        return AuthenticatedUser(
            uid=decoded["uid"],
            email=decoded.get("email"),
            claims=decoded,
            role=decoded.get("role"),
        )

    def _log_failure(self, reason: str) -> None:
        # uid is deliberately not included here: at this point the token
        # has failed verification, so nothing in it is trustworthy enough
        # to log as "this uid attempted to sign in" - see verify_token's
        # success-path log for where uid is safe to record (post-verification).
        logger.info(
            "token_verification_failed",
            extra={"event": _EVENT_TOKEN_VERIFICATION_FAILED, "reason": reason},
        )

    def authenticate_with_password(self, email: str, password: str) -> PasswordAuthResult:
        app = self._get_app()
        if not self._web_api_key:
            raise FirebaseNotConfiguredError("Firebase Web API key is not configured.")

        try:
            response = httpx.post(
                _SIGN_IN_WITH_PASSWORD_URL,
                params={"key": self._web_api_key},
                json={"email": email, "password": password, "returnSecureToken": True},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "password_auth_upstream_error",
                extra={"event": _EVENT_PASSWORD_AUTH_FAILED, "reason": "upstream_error"},
            )
            raise IdentityProviderUnavailableError(
                "Could not reach the identity provider. Try again."
            ) from exc

        if response.status_code != 200:
            # Firebase's own API deliberately collapses "no such account"
            # and "wrong password" into one generic error (enumeration
            # protection) - preserved here rather than trying to recover
            # which case it was.
            logger.info(
                "password_auth_failed",
                extra={"event": _EVENT_PASSWORD_AUTH_FAILED, "reason": "bad_credentials"},
            )
            raise UnauthorizedError("Incorrect email or password.")

        uid = response.json()["localId"]
        custom_token = firebase_auth.create_custom_token(uid, app=app)

        logger.info(
            "password_auth_succeeded",
            extra={"event": _EVENT_PASSWORD_AUTH_SUCCEEDED, "uid": uid},
        )
        return PasswordAuthResult(custom_token=custom_token.decode("utf-8"))

    def reset(self) -> None:
        """Test helper: tears down the cached Firebase app so a fresh
        instance re-initializes cleanly."""
        if self._app is not None:
            firebase_admin.delete_app(self._app)
        self._app = None
