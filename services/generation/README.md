# generation service

Stateless LLM question generation for nool-core. Only `core` calls it; it
has no database and no user data - core sends everything a request needs.

## What it does

`POST /v1/homework/gap-fill` - writes questions for the homework slots the
question bank couldn't fill (core: `services/homework_builder.py` →
`services/question_selector.py` first). A LlamaIndex Workflow
(`src/services/gap_fill.py`):

```
start → generate (one LLM call per slot, in parallel)
      → verify   (one call checks the batch: correct, grounded, right Bloom/difficulty,
                  self-contained, not a copy)
      → failed slots regenerate once with the verifier's reason → verify → stop
```

Slots that still fail come back in `unfilled_slot_indexes`, never as an
unverified question; core then falls back to the bank / legacy generator.

Core calls `/v1/voice-test/prep` from `services/voice_test_prep.py`, in the
background after a teacher creates a voice test; the result becomes the
test's `voice_test_questions` rows (the same questions for every student).

`POST /v1/voice-test/prep` - readies the voice-test questions core picked
from the school's datasets (one candidate per Bloom level): rewords each in
simple Indian classroom English (same meaning, answer, Bloom level and
difficulty), writes `spoken_text` (symbols in words) and a rubric
(`expected_points`, `misconceptions`, two `hints`). Same workflow shape
(`src/services/voice_prep.py`): gpt-5.1 rewords → code checks (sentence
length, no changed/added numbers, kept numbers keep units, banned words,
no "write/draw/tabulate/any four" asks) + gpt-5-mini answers the reworded
question and compares with the dataset answer → failed items regenerate
once → still failing: original wording, rubric kept if well-formed. One
output per requested level, in `levels` order.

## Config

| Env | Default | |
|---|---|---|
| `OPENAI_API_KEY` | – | without it `/ready` is 503 and generation answers 503 |
| `GENERATION_MODEL` | `gpt-5.1` | writes questions |
| `VERIFY_MODEL` | `gpt-5-mini` | checks them |

The LLM sits behind one protocol (`src/services/llm.py`), so tests use a
fake and a provider swap is one class.

## Run / test

```bash
docker compose up -d generation          # http://localhost:8005/docs
cd services/generation && pip install -r requirements-dev.txt && pytest
```
