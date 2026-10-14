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
    # A lighter writer for one task, without touching the rest - TASK_MODELS as JSON,
    # e.g. {"gap_fill": "gpt-5-mini"}. Tasks: gap_fill, voice_prep, paper, answer_key,
    # special_questions. Only switch one after its eval holds up on the lighter model.
    task_models: dict[str, str] = {}
    request_timeout_seconds: float = 170.0

    def writer_model(self, task: str) -> str:
        return self.task_models.get(task) or self.generation_model


def get_settings() -> GenerationSettings:
    """Reads settings from the environment on every call so tests can vary
    env vars per-case without dealing with cache invalidation.
    """
    return GenerationSettings()
