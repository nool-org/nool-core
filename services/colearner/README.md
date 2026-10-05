# Colearner Service

Voice Bloom's Taxonomy oral co-learner ("Milo"), ported from the
`nool-research` prototype (`nool-research/app`) into nool-core's service
shape. Firebase-authenticated, same as `auth`/`core`.

```
nool-apps -> NGINX -> Core -> Colearner Service -> OpenAI Realtime / OpenAI / Gemini / Edge-TTS
```

## Pipelines, each with fixed (non-configurable) models

| Pipeline | STT | LLM | TTS |
| --- | --- | --- | --- |
| **Realtime** (`/api/v1/colearner/realtime/calls*`) - what nool-apps uses | `gpt-4o-mini-transcribe` (transcript only) | `gpt-realtime-2.1-mini` speech-to-speech over WebRTC (voice `marin`) | (same model) |
| **Chained** (`/api/v1/colearner/chained*`) | OpenAI Whisper (`whisper-1`) | OpenAI `gpt-4o-mini` | Edge-TTS |
| **Multimodal** (`/api/v1/colearner/multimodal*`) | — (direct audio-in) | Gemini `gemini-3.5-flash-lite` | Edge-TTS |
| **Cascaded** prototype (`/api/v1/colearner/cascaded`) - cost/latency comparison, playground only | `gpt-4o-mini-transcribe` | `gpt-4.1-mini` | `gpt-4o-mini-tts` |

Every pipeline's Bloom report is built by `chained_service.generate_bloom_report`
(`gpt-4o-mini`, JSON mode). `POST /api/v1/homework-insight` (Core's
Homework details screen) also uses `gpt-4o-mini`, with a deterministic
fallback paragraph if the call fails.

### Realtime flow

The app sends its WebRTC SDP offer to Core, Core forwards it with the
session config to `POST /api/v1/colearner/realtime/calls`; colearner creates
the OpenAI Realtime call (instructions, `end_session` tool, server VAD)
and returns the SDP answer, so the app never sees the API key or prompt.
Colearner then attaches a sideband WebSocket to the same call and records
the transcript itself; `POST .../realtime/calls/{call_id}/finish` builds the
report from that server-side transcript, never from anything the client
sends. After the time limit the model gets a 30 s grace to wrap up, then
colearner hangs up. See `src/services/realtime_service.py`.

Model/provider/voice selection is intentionally **not** part of the request
schema (`src/domain/schemas.py`'s `SessionConfig`) - each pipeline's
service module (`src/services/chained_service.py`,
`src/services/multimodal_service.py`) fixes its own `LLM_MODEL`/`TTS_VOICE`
constants. Only pedagogical config (grade, subject, chapter, num_questions,
Bloom's levels, textbook context, reference questions, session time limit)
is client-supplied.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Process health. |
| GET | `/ready` | Ready when Firebase is configured and at least one model API key is set. |
| POST | `/api/v1/colearner/chained` | Chained pipeline turn (init + subsequent turns). Requires `Authorization: Bearer <firebase-id-token>`. |
| WS | `/api/v1/colearner/chained/stream?token=<firebase-id-token>` | Chained pipeline, live streaming. |
| POST | `/api/v1/colearner/multimodal` | Multimodal pipeline turn. Requires `Authorization: Bearer <firebase-id-token>`. |
| WS | `/api/v1/colearner/multimodal/stream?token=<firebase-id-token>` | Multimodal pipeline, live streaming. |
| POST | `/api/v1/colearner/realtime/calls` | SDP offer + `SessionConfig` + `owner_ref` in, SDP answer + `call_id` out. |
| POST | `/api/v1/colearner/realtime/calls/{call_id}/finish` | Ends the call, returns the Bloom report (same `owner_ref` required). |
| POST | `/api/v1/colearner/cascaded` | Cascaded prototype turn. |
| POST | `/api/v1/homework-insight` | Homework "why / how to close the gap" content. |
| GET | `/api/v1/colearner/playground` | Dev-only browser test page; 404 unless `PLAYGROUND_ENABLED`. |

## Playground (dev only)

`src/playground/index.html`, served at `/api/v1/colearner/playground` when
`PLAYGROUND_ENABLED` is true (`compose.override.yml` sets it locally:
`http://localhost:8004/api/v1/colearner/playground`). With the flag on,
requests **without** an `Authorization` header are accepted as a fixed
playground user, so the page needs no sign-in. Real model calls are made
and billed. Never enable it in production.

WebSocket auth uses a `?token=` query param rather than an `Authorization`
header, since browsers cannot set custom headers on the WS handshake - same
pattern as `services/core/src/api/routes/ai_assessor.py`'s
`stream_session`.

## Known v1 limitations

- **Sessions and live realtime calls are in-memory only**
  (`src/services/session_store.py`, `realtime_service.py`) - lost
  on container restart, not shared across replicas, so run exactly one
  colearner replica. No Redis/DB added for
  this; revisit only if the service needs to scale horizontally or survive
  restarts mid-session (see nool-core/claude.md: don't add infra without an
  actual requirement).
- **Reports are written to a container-local `reports/` directory**, not a
  durable store - same reasoning as above.

## Environment variables

See the repo root `.env.example`'s "Voice Co-Learner service" section:
`FIREBASE_PROJECT_ID`, `FIREBASE_CREDENTIALS_HOST_PATH` (shared with
`auth`/`core`), `OPENAI_API_KEY` (shared with `kg`), `GEMINI_API_KEY`,
`PLAYGROUND_ENABLED` (dev only, default off).

## Tests

`pytest` (see `docs/DEVELOPMENT.md`'s "Tests"); model clients are faked, so
no API key is needed or wanted.
