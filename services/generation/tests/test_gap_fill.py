"""Gap-fill workflow with a scripted fake LLM - no network."""

import pytest
from fastapi.testclient import TestClient

from src.services.gap_fill import GapFillRequest, Question, _Drafts, _Verdict, _Verdicts, fill_gaps

REQUEST = GapFillRequest(
    subject="Science", grade=10, chapter="Electricity",
    sections=[{"title": "11.6.1 Resistors in Series", "text": "In series, R = R1 + R2 + R3 ..."}],
    slots=[{"bloom_level": "APPLY", "difficulty": "HARD"}, {"bloom_level": "UNDERSTAND", "difficulty": "MEDIUM"}],
    examples=[{"text": "Two resistors of 2 ohm and 4 ohm are in series...", "answer": "6 ohm"}],
)


def question(i, text="q"):
    return Question(slot_index=i, question_type="numerical", text=f"{text}{i}", options=[], answer="a",
                    solution="s", explanation="", hint="",
                    grounded_in="11.6.1 Resistors in Series")


class FakeLLM:
    """Replays scripted outputs per output type and records every call."""

    def __init__(self, drafts, verdicts):
        self.script = {_Drafts: list(drafts), _Verdicts: list(verdicts)}
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append((output_type.__name__, model, user))
        return self.script[output_type].pop(0)


def ok(*idx):
    return [_Verdict(slot_index=i, ok=True, problem="") for i in idx]


@pytest.mark.asyncio
async def test_all_slots_pass_verification_first_time():
    llm = FakeLLM([_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)])], [_Verdicts(verdicts=ok(0, 1))])
    result = await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.slot_index for q in result.questions] == [0, 1]
    assert result.unfilled_slot_indexes == [] and result.attempts == 1
    # one generation call per slot (in parallel), then one verification of the batch
    assert [c[:2] for c in llm.calls] == [("_Drafts", "gen"), ("_Drafts", "gen"), ("_Verdicts", "ver")]


@pytest.mark.asyncio
async def test_a_rejected_slot_is_regenerated_once_with_the_verifiers_reason():
    llm = FakeLLM(
        [_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)]), _Drafts(questions=[question(1, "fixed")])],
        [_Verdicts(verdicts=ok(0) + [_Verdict(slot_index=1, ok=False, problem="answer is wrong")]),
         _Verdicts(verdicts=ok(1))])
    result = await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.text for q in result.questions] == ["q0", "fixed1"] and result.attempts == 2
    regenerate_prompt = llm.calls[3][2]  # gen, gen, verify, then only slot 1 again
    assert '"feedback": "answer is wrong"' in regenerate_prompt and '"slot_index": 0' not in regenerate_prompt
    assert len(llm.calls) == 5


@pytest.mark.asyncio
async def test_slots_failing_twice_or_never_drafted_come_back_unfilled():
    llm = FakeLLM(
        [_Drafts(questions=[question(0)]), _Drafts(questions=[]),   # slot 1 never drafted
         _Drafts(questions=[question(1)])],
        [_Verdicts(verdicts=[_Verdict(slot_index=0, ok=True, problem="")]),
         _Verdicts(verdicts=[_Verdict(slot_index=1, ok=False, problem="refers to a figure")])])
    result = await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.slot_index for q in result.questions] == [0]
    assert result.unfilled_slot_indexes == [1]


def test_api_answers_503_without_a_key_and_runs_with_one(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from src.main import create_app

    app = create_app()
    with TestClient(app) as client:
        body = REQUEST.model_dump()
        assert client.post("/v1/homework/gap-fill", json=body).status_code == 503
        assert client.get("/ready").status_code == 503
        app.state.llm = FakeLLM([_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)])],
                                [_Verdicts(verdicts=ok(0, 1))])
        response = client.post("/v1/homework/gap-fill", json=body)
        assert response.status_code == 200 and len(response.json()["questions"]) == 2


@pytest.mark.asyncio
async def test_one_slot_erroring_does_not_sink_the_others():
    class Flaky(FakeLLM):
        async def __call__(self, **kw):
            if kw["output_type"] is _Drafts and '"slot_index": 1' in kw["user"]:
                raise RuntimeError("provider hiccup")
            return await super().__call__(**kw)

    llm = Flaky([_Drafts(questions=[question(0)])], [_Verdicts(verdicts=ok(0)), _Verdicts(verdicts=[])])
    result = await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.slot_index for q in result.questions] == [0] and result.unfilled_slot_indexes == [1]


@pytest.mark.asyncio
async def test_a_failing_verifier_keeps_what_was_already_accepted_and_the_retry_avoids_it():
    class VerifierDown(FakeLLM):
        async def __call__(self, **kw):
            if kw["output_type"] is _Verdicts and len(self.calls) > 3:   # the retry's verification
                self.calls.append(("_Verdicts", kw["model"], kw["user"]))
                raise TimeoutError
            return await super().__call__(**kw)

    llm = VerifierDown(
        [_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)]), _Drafts(questions=[question(1, "again")])],
        [_Verdicts(verdicts=ok(0) + [_Verdict(slot_index=1, ok=False, problem="too easy")])])
    result = await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.text for q in result.questions] == ["q0"] and result.unfilled_slot_indexes == [1]
    assert '"avoid": ["q0"]' in llm.calls[3][2]      # the regenerate prompt


@pytest.mark.asyncio
async def test_a_malformed_mcq_is_rejected_without_asking_the_verifier():
    bad = Question(slot_index=0, question_type="mcq", text="Which?", options=["a", "b", " "], answer="a",
                   solution="s", explanation="e", hint="h", grounded_in="x")
    llm = FakeLLM([_Drafts(questions=[bad]), _Drafts(questions=[question(1)]), _Drafts(questions=[question(0)])],
                  [_Verdicts(verdicts=ok(1)), _Verdicts(verdicts=ok(0))])
    result = await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.text for q in result.questions] == ["q0", "q1"]
    assert "exactly 4 distinct" in llm.calls[3][2]   # fed back to the regenerate call


@pytest.mark.asyncio
async def test_an_mcq_slot_gets_its_concept_and_misconceptions_and_rejects_a_non_mcq():
    request = GapFillRequest(
        subject="Science", grade=10, chapter="Electricity", sections=REQUEST.sections,
        slots=[{"bloom_level": "UNDERSTAND", "difficulty": "MEDIUM", "question_type": "mcq", "concept": "current",
                "misconceptions": ["current is used up in a bulb"]}])
    mcq = Question(slot_index=0, question_type="mcq", text="Which is true?", options=["a", "b", "c", "d"], answer="a",
                   solution="s", explanation="e", hint="h", grounded_in="x")
    llm = FakeLLM([_Drafts(questions=[question(0)]), _Drafts(questions=[mcq])], [_Verdicts(verdicts=ok(0))])
    result = await fill_gaps(llm, request, generation_model="gen", verify_model="ver")
    assert [q.question_type for q in result.questions] == ["mcq"]
    assert '"concept": "current"' in llm.calls[0][2] and "current is used up in a bulb" in llm.calls[0][2]
    assert "needs an mcq" in llm.calls[1][2]  # the numerical draft was rejected in code, fed back


@pytest.mark.asyncio
async def test_homework_slots_are_sent_without_the_mcq_fields():
    llm = FakeLLM([_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)])], [_Verdicts(verdicts=ok(0, 1))])
    await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert all('"misconceptions"' not in c[2] and '"concept"' not in c[2] for c in llm.calls[:2])


@pytest.mark.asyncio
async def test_spoken_style_asks_for_one_short_question():
    systems = []

    class Recording(FakeLLM):
        async def __call__(self, *, model, effort, system, user, output_type):
            systems.append((output_type.__name__, system))
            return await super().__call__(model=model, effort=effort, system=system, user=user, output_type=output_type)

    spoken = REQUEST.model_copy(update={"style": "spoken"})
    llm = Recording([_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)])], [_Verdicts(verdicts=ok(0, 1))])
    await fill_gaps(llm, spoken, generation_model="gen", verify_model="ver")
    assert all("ASKED ALOUD" in s for name, s in systems if name == "_Drafts")

    llm = Recording([_Drafts(questions=[question(0)]), _Drafts(questions=[question(1)])], [_Verdicts(verdicts=ok(0, 1))])
    systems.clear()
    await fill_gaps(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert not any("ASKED ALOUD" in s for _, s in systems)  # homework (written) is unchanged


@pytest.mark.asyncio
async def test_an_mcq_hint_that_gives_the_answer_away_is_rejected_in_code():
    request = GapFillRequest(subject="Science", grade=10, chapter="Electricity", sections=REQUEST.sections,
                             slots=[{"bloom_level": "ANALYZE", "difficulty": "HARD", "question_type": "mcq"}])

    def mcq(hint, explanation="Series resistances add up."):
        return Question(slot_index=0, question_type="mcq", text="Total resistance?", options=["6 ohm", "2 ohm", "8 ohm", "3 ohm"],
                        answer="6 ohm", solution="s", explanation=explanation, hint=hint, grounded_in="x")
    llm = FakeLLM([_Drafts(questions=[mcq("It is 6 Ohm, add them.")]), _Drafts(questions=[mcq("What happens to R in series?")])],
                  [_Verdicts(verdicts=ok(0))])
    result = await fill_gaps(llm, request, generation_model="gen", verify_model="ver")
    [q] = result.questions
    assert q.hint == "What happens to R in series?" and q.explanation == "Series resistances add up."
    assert "never contains the right option" in llm.calls[1][2]
    llm = FakeLLM([_Drafts(questions=[mcq("Think series.", explanation=" ")])] * 2, [])
    result = await fill_gaps(llm, request, generation_model="gen", verify_model="ver")
    assert result.unfilled_slot_indexes == [0] and "one-line explanation" in llm.calls[1][2]


@pytest.mark.asyncio
async def test_maths_gets_problem_first_rules_science_does_not():
    systems = []

    class Recording(FakeLLM):
        async def __call__(self, *, model, effort, system, user, output_type):
            systems.append((output_type.__name__, system))
            return await super().__call__(model=model, effort=effort, system=system, user=user,
                                          output_type=output_type)

    maths = REQUEST.model_copy(update={"subject": "Mathematics", "slots": REQUEST.slots[:1]})
    await fill_gaps(Recording([_Drafts(questions=[question(0)])], [_Verdicts(verdicts=ok(0))]), maths,
                    generation_model="gen", verify_model="ver")
    writer = next(s for t, s in systems if t == "_Drafts")
    assert "write PROBLEMS, not text questions" in writer and "never LaTeX" in writer
    systems.clear()
    science = REQUEST.model_copy(update={"slots": REQUEST.slots[:1]})
    await fill_gaps(Recording([_Drafts(questions=[question(0)])], [_Verdicts(verdicts=ok(0))]), science,
                    generation_model="gen", verify_model="ver")
    assert "PROBLEMS" not in next(s for t, s in systems if t == "_Drafts")
