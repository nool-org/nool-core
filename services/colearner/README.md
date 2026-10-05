# Colearner Service

Voice Bloom's Taxonomy oral co-learner ("Milo"), ported from the
`nool-research` prototype (`nool-research/app`) into nool-core's service
shape. Firebase-authenticated, same as `auth`/`core`.

```
nool-apps -> NGINX -> Core -> Colearner Service -> OpenAI (Realtime, transcribe, gpt-5-mini)
```

Realtime is the only production voice path. The per-turn chained
(Whisper/gpt-4o-mini/Edge-TTS) and multimodal (Gemini) pipelines were
removed in wave 2; `src/services/chained_service.py` keeps only the shared
co-learner prompt and the cascaded prototype's report helper.

## Pipelines and models (fixed in code, never client-selectable)

| Pipeline / feature | STT | LLM | TTS |
| --- | --- | --- | --- |
| **Realtime** (`/api/v1/colearner/realtime/calls*`) - what nool-apps uses | `gpt-4o-mini-transcribe` (transcript only) | `gpt-realtime-2.1-mini` speech-to-speech over WebRTC (voice `marin`) | (same model) |
| **Cascaded** prototype (`/api/v1/colearner/cascaded`) - cost/latency comparison, playground only | `gpt-4o-mini-transcribe` | `gpt-4.1-mini` | `gpt-4o-mini-tts` |

Grading (`src/services/evaluation/grader.py`, `grade_transcript`) is
`gpt-5-mini` with a strict JSON schema: the model grades each answer (score,
points covered, misconception, reason, student response, hints used,
`is_substantively_correct`), and mastery / per-Bloom means / the hint
penalty are computed in code. With a per-question rubric (from the prepared
questions) it grades against each question's expected points and known
misconceptions. If grading fails the report carries `grading_error` and
Core stores the session for teacher review instead of a score.

`POST /api/v1/homework-insight` (Core's Homework details screen) uses
`gpt-4o-mini`, with a deterministic fallback paragraph if the call fails.

### Realtime flow

The app sends its WebRTC SDP offer to Core, Core forwards it with the
session config to `POST /api/v1/colearner/realtime/calls`; colearner creates
the OpenAI Realtime call (instructions, tools, server VAD) and returns the
SDP answer, so the app never sees the API key or prompt. Colearner then
attaches a sideband WebSocket to the same call and records the transcript
itself.

With `config.questions` (the test's prepared questions, contract C3) Milo
asks exactly those, in order, through tools: `ask_question` (returns the
question's `spoken_text`), `give_hint` (the rubric's hints, max 2 per
question, then "encourage instead"), `give_explanation` (the expected
points) and `end_session`. The sideband tags each student turn with its
question (first answer vs. follow-up / hint / explanation request). Without
questions Milo improvises from `textbook_context`.

`POST .../realtime/calls/{call_id}/finish` grades that server-side
transcript, never anything the client sends, and returns `report`,
`answers`, a question-tagged `transcript` (`turns` kept as an alias),
`answered` and `model_completed`. After `end_session` or the time limit the
model gets a 30 s grace to wrap up, then colearner hangs up. See
`src/services/realtime_service.py`.

Model/voice selection is intentionally **not** part of the request schema
(`src/domain/schemas.py`'s `SessionConfig`) - `realtime_service.py` and
`cascaded_service.py` fix their own model constants. Only pedagogical
config (grade, subject, chapter, num_questions, Bloom's levels, textbook
context, questions, session time limit) is client-supplied.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Process health. |
| GET | `/ready` | Ready when Firebase is configured and `OPENAI_API_KEY` is set. |
| POST | `/api/v1/colearner/realtime/calls` | SDP offer + `SessionConfig` + `owner_ref` in, SDP answer + `call_id` out. |
| POST | `/api/v1/colearner/realtime/calls/{call_id}/finish` | Ends the call, returns the graded report (same `owner_ref` required). |
| POST | `/api/v1/colearner/cascaded` | Cascaded prototype turn. |
| POST | `/api/v1/homework-insight` | Homework "why / how to close the gap" content. |
| GET | `/api/v1/colearner/playground` | Dev-only browser test page; 404 unless `PLAYGROUND_ENABLED`. |

All but `/health`, `/ready` and the playground page require
`Authorization: Bearer <firebase-id-token>`.

## Grader accuracy gate

`evals/` holds a labelled transcript set and `grader_eval.py`, which runs
the real grader and fails if verdict accuracy is under 95% or more than 1
in 30 wrong answers are marked correct. Run it (real API, about $0.09)
after any change to the grader prompt, schema or model - see
`evals/README.md`.

## Playground (dev only)

`src/playground/index.html` - Realtime and Cascaded modes - served at `/api/v1/colearner/playground` when
`PLAYGROUND_ENABLED` is true (`compose.override.yml` sets it locally:
`http://localhost:8004/api/v1/colearner/playground`). With the flag on,
requests **without** an `Authorization` header are accepted as a fixed
playground user, so the page needs no sign-in. Real model calls are made
and billed. Never enable it in production.

## Known v1 limitations

- **Sessions and live realtime calls are in-memory only**
  (`src/services/session_store.py`, `realtime_service.py`) - lost
  on container restart, not shared across replicas, so run exactly one
  colearner replica. No Redis/DB added for
  this; revisit only if the service needs to scale horizontally or survive
  restarts mid-session (see nool-core/claude.md: don't add infra without an
  actual requirement).
- **Transcript/report files are written to a container-local `reports/` directory**, not a
  durable store - same reasoning as above.

## Environment variables

See the repo root `.env.example`'s "Voice Co-Learner service" section:
`FIREBASE_PROJECT_ID`, `FIREBASE_CREDENTIALS_HOST_PATH` (shared with
`auth`/`core`), `OPENAI_API_KEY` (shared with `kg`/`generation`),
`PLAYGROUND_ENABLED` (dev only, default off).

## Tests

`pytest` (see `docs/DEVELOPMENT.md`'s "Tests"); model clients are faked, so
no API key is needed or wanted.
