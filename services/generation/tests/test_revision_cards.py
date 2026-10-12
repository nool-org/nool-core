import asyncio

from fastapi.testclient import TestClient

from src.services import revision_cards as svc

REQUEST = svc.CardsRequest(grade=10, subject="Science", chapter="Light", topic="Refraction",
                           context="Light bends when it passes from one medium to another...")


def card(kind="concept", body="Light bends at the boundary of two media.", answer=None):
    return {"kind": kind, "title": "Refraction", "body": body, "answer": answer}


def deck(n=6, check_answer="Towards the normal."):
    cards = [card("formula", "Snell's law: $n_1 \\sin i = n_2 \\sin r$."), card("mistake")]
    cards += [card() for _ in range(n - 3)]
    return cards + [card("check", "Which way does light bend entering glass?", check_answer)]


class FakeLLM:
    def __init__(self, *decks):
        self.decks = list(decks)
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append({"model": model, "effort": effort, "system": system, "user": user})
        return output_type.model_validate({"cards": self.decks.pop(0)})


def test_writes_a_grounded_deck_with_a_low_effort_call():
    llm = FakeLLM(deck())
    result = asyncio.run(svc.write_cards(llm, REQUEST, model="gpt-5-mini"))
    assert len(result.cards) == 6 and result.cards[-1].answer == "Towards the normal."
    assert all(c.answer is None for c in result.cards[:-1])
    assert llm.calls[0]["effort"] == "low" and "Light bends when it passes" in llm.calls[0]["user"]
    assert 'chapter "Light"' in llm.calls[0]["system"]


def test_code_checks():
    ok = [svc.Card(**c) for c in deck()]
    assert svc.problems(ok) == []
    assert svc.problems(ok[:4])  # too few
    assert svc.problems([svc.Card(**c) for c in deck(n=9)])  # too many
    assert svc.problems([svc.Card(**c) for c in deck(check_answer=None)])  # check card without its answer
    assert svc.problems([*ok[:-1], svc.Card(**card(body=""))])  # empty, and no check card left
    assert svc.problems([svc.Card(**card(body="x" * 401)), *ok[1:]])
    assert svc.problems([svc.Card(**card(body="Draw a ray diagram for it.")), *ok[1:]])
    assert svc.problems([*ok[:-1], svc.Card(**card("check", "Can you draw the ray?", "Yes"))])
    # a mistake card may mention writing - it isn't an instruction
    assert svc.problems([svc.Card(**card("mistake", "Students often write sin r first.")), *ok[1:]]) == []


def test_retries_once_with_the_problems_then_gives_up():
    llm = FakeLLM(deck(n=3), deck())
    assert len(asyncio.run(svc.write_cards(llm, REQUEST, model="m")).cards) == 6
    assert "write 6-8 cards, not 3" in llm.calls[1]["user"]

    llm = FakeLLM(deck(n=3), deck(n=3))
    try:
        asyncio.run(svc.write_cards(llm, REQUEST, model="m"))
        raise AssertionError("expected RevisionCardsError")
    except svc.RevisionCardsError:
        pass


def test_api_shape(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from src.main import create_app

    app = create_app()
    with TestClient(app) as client:
        body = REQUEST.model_dump()
        assert client.post("/v1/revision/cards", json=body).status_code == 503
        app.state.llm = FakeLLM(deck())
        response = client.post("/v1/revision/cards", json=body)
        assert response.status_code == 200
        cards = response.json()["cards"]
        assert len(cards) == 6 and set(cards[0]) == {"kind", "title", "body", "answer"}
        app.state.llm = FakeLLM(deck(n=3), deck(n=3))
        assert client.post("/v1/revision/cards", json=body).status_code == 502
