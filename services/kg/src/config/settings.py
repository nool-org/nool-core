from shared.config import BaseServiceSettings


class KgSettings(BaseServiceSettings):
    service_name: str = "kg"

    # nool-kg (FalkorDB-backed textbook graph) is the only KG this service
    # talks to - see services/core/src/services/kg_client.py for the HTTP
    # seam everything else in nool-core goes through instead.
    nool_kg_base_url: str = "https://nool-data.draco-labs.in"

    # Question generation grounded in the KG - see src/services/generator.py.
    openai_api_key: str | None = None


def get_settings() -> KgSettings:
    """Reads settings from the environment on every call so tests can vary
    env vars per-case without dealing with cache invalidation.
    """
    return KgSettings()
