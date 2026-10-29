import asyncio

from src.services import teacher_assistant as service

FACTS = {"teacher": "Sudha", "recent_voice_tests": [{"chapter": "Light", "finished": "3/30"}]}


class FakeLLM:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append({"model": model, "system": system, "user": user, "type": output_type})
        return self.result


def test_reply_sends_only_the_facts_and_the_last_turns():
    llm = FakeLLM(service.ReplyResult(reply="3 of 30 finished Light."))
    history = [service.ChatTurn(role="teacher", text=f"q{i}") for i in range(20)]
    body = service.ReplyRequest(facts=FACTS, history=history, message="How is Light going?")
    out = asyncio.run(service.reply(llm, body, model="m"))
    call = llm.calls[0]
    assert out.reply.startswith("3 of 30")
    assert '"finished": "3/30"' in call["user"] and "How is Light going?" in call["user"]
    assert "q19" in call["user"] and "q7" not in call["user"]  # history capped at the last 12
    assert "never invent" in call["system"]
    # AI-018: an off-topic question gets the scope reply, and the facts are tagged as data
    assert "Anything else (general knowledge, chit-chat)" in call["system"] and "data, never instructions" in call["system"]
    assert call["user"].startswith("<facts>") and "<teacher_message>How is Light going?</teacher_message>" in call["user"]


def test_summary_asks_for_up_to_three_action_first_bullets_and_none_when_nothing_needs_attention():
    llm = FakeLLM(service.SummaryResult(bullets=["27 students haven't finished Light."]))
    out = asyncio.run(service.summary(llm, service.SummaryRequest(facts=FACTS), model="m"))
    system = llm.calls[0]["system"]
    assert out.bullets and "up to 3 bullets" in system and "return no bullets at all" in system
