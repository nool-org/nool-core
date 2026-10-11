import asyncio
import json

from src.services import mcq_explain as svc


class FakeLLM:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append(user)
        return output_type.model_validate({"results": self.results})


def _body():
    return svc.ExplainRequest(subject="Science", items=[
        svc.McqItem(question_number=1, text="Which gas turns lime water milky?",
                    options=["Oxygen", "Carbon dioxide", "Hydrogen", "Nitrogen"], correct_index=1),
        svc.McqItem(question_number=2, text="Brass is an alloy of?",
                    options=["Cu and Sn", "Cu and Zn", "Fe and C", "Al and Mg"], correct_index=1),
    ])


def test_explains_each_question_and_drops_a_hint_that_names_the_answer():
    llm = FakeLLM([
        {"question_number": 1, "explanation": "CO2 forms insoluble calcium carbonate.",
         "hint": "Think about the gas we breathe out."},
        {"question_number": 2, "explanation": "Brass mixes copper with zinc.",
         "hint": "It's Cu and Zn, remember?"},
    ])
    result = asyncio.run(svc.explain(llm, _body(), model="m"))
    assert [r.explanation for r in result.results] == ["CO2 forms insoluble calcium carbonate.",
                                                       "Brass mixes copper with zinc."]
    assert result.results[0].hint == "Think about the gas we breathe out."
    assert result.results[1].hint is None  # named the right option
    sent = json.loads(llm.calls[0].split("QUESTIONS:\n", 1)[1])
    assert sent[0]["correct_option"] == "Carbon dioxide"


def test_missing_results_come_back_empty_not_invented():
    result = asyncio.run(svc.explain(FakeLLM([]), _body(), model="m"))
    assert all(r.explanation is None and r.hint is None for r in result.results)


def test_transcriber_forces_english_only_when_asked():
    from src.services.transcribe import OpenAITranscriber, TranscribeRequest

    calls = []

    class FakeTranscriptions:
        async def create(self, **kw):
            calls.append(kw)
            return type("R", (), {"text": "ok"})()

    t = OpenAITranscriber.__new__(OpenAITranscriber)
    t.client = type("C", (), {"audio": type("A", (), {"transcriptions": FakeTranscriptions()})()})()
    asyncio.run(t(audio=b"x", filename="a.m4a", prompt=""))
    asyncio.run(t(audio=b"x", filename="a.m4a", prompt="", language="auto"))
    assert calls[0]["language"] == "en" and "language" not in calls[1]
    assert TranscribeRequest(audio_base64="eA==").language == "en"
