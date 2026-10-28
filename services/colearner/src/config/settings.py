
from shared.config import BaseServiceSettings


class ColearnerSettings(BaseServiceSettings):
    service_name: str = "colearner"

    # Firebase - same shape as auth/core's settings (see
    # services/auth/src/config/settings.py). Token verification only; this
    # service never issues tokens or checks passwords.
    firebase_project_id: str | None = None
    firebase_credentials_path: str | None = None
    # Dev-only: serves the playground page AND lets requests without an
    # Authorization header through (see src/api/deps.py). Only
    # compose.override.yml turns it on - never set it in a deployed env.
    playground_enabled: bool = False

    # OpenAI is the only model provider (realtime, cascaded, grader,
    # homework insight); never client-supplied.
    openai_api_key: str | None = None


def get_settings() -> ColearnerSettings:
    """Reads settings from the environment on every call so tests can vary
    env vars per-case without dealing with cache invalidation.
    """
    return ColearnerSettings()
