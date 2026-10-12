from fastapi import Header, Request

from shared.auth import AuthenticatedUser, verify_token
from shared.errors import UnauthorizedError

from src.config.settings import ColearnerSettings, get_settings

_PLAYGROUND_USER = AuthenticatedUser(uid="playground", email=None, claims={})


def playground_active(settings: ColearnerSettings) -> bool:
    """PLAYGROUND_ENABLED is dev-only: it is ignored outright when
    ENVIRONMENT=production, so a stray flag can never open the no-auth path."""
    return settings.playground_enabled and settings.environment.lower() != "production"


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header.")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("Missing or malformed Authorization header.")
    return token


def nool_caller(request: Request) -> dict[str, str]:
    """Core's X-Nool-* headers (feature, student, teacher, school) - context
    for the cost log lines only, never trusted for anything else."""
    found = {k: (request.headers.get(f"x-nool-{k}") or "")[:64] for k in ("feature", "student", "teacher", "school")}
    return {k: v for k, v in found.items() if v}


def get_current_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    """FastAPI dependency: extracts and verifies the caller's Firebase ID
    token. No DB/role resolution here (unlike services/core/src/api/deps.py's
    get_current_app_user) - this service only needs "is the caller
    authenticated," not a resolved application profile.
    """
    # A token that IS sent is still verified - only "no header" is waved through.
    if not authorization and playground_active(get_settings()):
        return _PLAYGROUND_USER
    token = _extract_bearer_token(authorization)
    return verify_token(token)
