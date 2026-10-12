import asyncio

from src.services import quiz_analysis as svc


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append({"model": model, "effort": effort, "system": system, "user": user})
        return output_type.model_validate(self.reply)


REPLY = {"summary": " Good recall. ", "lagging_meaning": "You know V = IR but not how to use it.",
         "need_to_know": ["a", "b", " ", "c", "d"], "next_step": "Do the homework."}


def request(**kw):
    return svc.AnalysisRequest(**{"grade": 10, "subject": "Science", "chapter": "Electricity", "topic": "Ohm's law",
                                  "attempt": 1, "per_bloom": {"REMEMBER": 100, "APPLY": 0}, "lagging_level": "APPLY",
                                  "missed": [{"bloom_level": "APPLY", "question": "A 2 ohm wire...",
                                              "chosen": "1 A", "correct": "3 A"}], **kw})


def test_writes_the_analysis_from_scores_and_missed_questions():
    llm = FakeLLM(REPLY)
    result = asyncio.run(svc.analyse(llm, request(), model="gpt-5-mini"))
    assert result.summary == "Good recall." and result.need_to_know == ["a", "b", "c"]
    call = llm.calls[0]
    assert call["effort"] == "low" and "Apply 0%" in call["user"] and "APPLY" not in call["user"] and "A 2 ohm wire" in call["user"]
    assert "homework" in call["system"] and '"Ohm\'s law"' in call["system"]


def test_nothing_lagging_and_recheck_change_the_ask():
    llm = FakeLLM(REPLY)
    result = asyncio.run(svc.analyse(llm, request(lagging_level=None, missed=[]), model="m"))
    assert result.lagging_meaning == "" and "next one" in llm.calls[0]["system"]
    asyncio.run(svc.analyse(llm, request(attempt=2, previous_per_bloom={"APPLY": 0}), model="m"))
    assert "First quiz, for comparison: Apply 0%" in llm.calls[1]["user"]
    assert "teacher" in llm.calls[1]["system"]
