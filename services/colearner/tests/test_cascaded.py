import asyncio

from src.domain.schemas import SessionConfig
from src.services import cascaded_service as cs
from src.services.session_store import session_store


def test_unheard_audio_is_never_a_student_turn(monkeypatch):
    """AI-017: an empty or prompt-echo transcript was stored as "(Inaudible audio response)" and graded."""
    heard = []

    class FakeTranscriptions:
        async def create(self, **kw):
            heard.append(kw["prompt"])
            return type("R", (), {"text": kw["prompt"]})()  # silence: the prompt comes back

    async def think(session):
        return "Can you say that again?", 0.1

    async def speak(session, text):
        return b"", 0.1, 0.0

    monkeypatch.setattr(cs, "openai_client", type("C", (), {"audio": type("A", (), {"transcriptions": FakeTranscriptions()})()})())
    monkeypatch.setattr(cs, "convert_audio_to_wav", lambda audio, rate: b"")
    monkeypatch.setattr(cs, "_think", think)
    monkeypatch.setattr(cs, "_speak", speak)
    session = session_store.create_session(SessionConfig())
    asyncio.run(cs.process_cascaded_turn(session))  # greeting
    result = asyncio.run(cs.process_cascaded_turn(session, user_audio_bytes=b"x"))
    assert heard and result["student_transcript"] is None
    assert [t for t in session.turns if t["role"] == "user"] == []
    assert session.openai_messages[-2]["role"] == "system"
