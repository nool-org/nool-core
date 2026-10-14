from src.config.settings import GenerationSettings


def test_writer_model_per_task(monkeypatch):
    monkeypatch.setenv("TASK_MODELS", '{"gap_fill": "gpt-5-mini"}')
    settings = GenerationSettings()
    assert settings.writer_model("gap_fill") == "gpt-5-mini"
    assert settings.writer_model("voice_prep") == settings.generation_model
