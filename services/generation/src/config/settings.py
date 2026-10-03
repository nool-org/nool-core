from shared.config import BaseServiceSettings


class GenerationSettings(BaseServiceSettings):
    service_name: str = "generation"

    # Blank-safe like the other services: without a key the service starts,
    # /ready reports not_ready and generation routes answer 503.
    openai_api_key: str | None = None
    # Writing a good question needs the stronger model; checking one is a
    # narrower task. Both overridable per environment.
    generation_model: str = "gpt-5.1"
    verify_model: str = "gpt-5-mini"
    request_timeout_seconds: float = 170.0


def get_settings() -> GenerationSettings:
    """Reads settings from the environment on every call so tests can vary
    env vars per-case without dealing with cache invalidation.
    """
    return GenerationSettings()
