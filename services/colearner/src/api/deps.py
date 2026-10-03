from fastapi import Header

from shared.auth import AuthenticatedUser, verify_token
from shared.errors import UnauthorizedError

from src.config.settings import get_settings

_PLAYGROUND_USER = AuthenticatedUser(uid="playground", email=None, claims={})


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header.")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("Missing or malformed Authorization header.")
    return token


def get_current_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    """FastAPI dependency: extracts and verifies the caller's Firebase ID
    token. No DB/role resolution here (unlike services/core/src/api/deps.py's
    get_current_app_user) - this service only needs "is the caller
    authenticated," not a resolved application profile.
    """
    # A token that IS sent is still verified - only "no header" is waved through.
    if not authorization and get_settings().playground_enabled:
        return _PLAYGROUND_USER
    token = _extract_bearer_token(authorization)
    return verify_token(token)
