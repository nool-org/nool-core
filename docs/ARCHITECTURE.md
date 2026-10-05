# Architecture

## Overview

nool-core is the backend platform for noolAI, an AI voice co-learner for
Indian school students: an authentication service, a core business API,
three AI services (colearner, generation, kg), an NGINX reverse proxy and
PostgreSQL, run together via Docker Compose for local development.
`nool-apps` and the admin web apps are separate repositories that talk to
this stack over HTTP.

```
                          NGINX (:8080)
          /auth/*  |      /api/*      |  /api/v1/colearner/*
                   v         v        v
                 Auth      Core ---> Colearner ---> OpenAI Realtime / OpenAI / Gemini / Edge-TTS
                   |         |                     |         |   +--> Generation ---> OpenAI (gpt-5.1, gpt-5-mini)
                   |         |   +--> KG ---------> nool-kg (FalkorDB textbook graph) + OpenAI (gpt-5.1)
                   +----+----+
                        v
                    PostgreSQL
```

Only NGINX is public. Core reaches colearner, generation and kg over the
Compose network (`COLEARNER_SERVICE_URL`, `GENERATION_SERVICE_URL`,
`KG_SERVICE_URL`); generation and kg are not routed through NGINX at all.

## Services

### NGINX

The external HTTP entry point. Routes `/auth/*` to the Auth service,
`/api/v1/colearner/*` to the Colearner service (a more specific prefix than
`/api/`, so it does not fall through to Core), and everything else under
`/api/*` to the Core service. Forwards/generates `X-Request-ID`, sets CORS
headers, and enforces request size limits. Contains no business or
authentication logic - see `nginx/`.

### Auth Service

Verifies the caller's token and extracts identity, via whatever identity
provider is configured behind `shared/auth`'s `IdentityProvider` interface
(see "Identity & Authorization" below):

```
nool-app -> NGINX -> Auth Service -> Firebase
```

Firebase is the identity provider today. This service never implements
password auth and never stores passwords. See `services/auth/README.md`.

### Core Service

Owns the product domain: Schools, Teachers, Students, Classes, Curriculum,
Voice Tests, Test Results, Homework, Journey, Practice Bank, Question Papers,
Dashboards, Assistant, AI Assessor sessions, Leaderboard, and the Super
Admin / School Admin surfaces - the full contract nool-app's
`spec/docs/api-reference.html` defines. See `services/core/README.md` for
the schema/route/authorization architecture.

### Colearner Service

The voice co-learner ("Milo"): runs a Bloom's Taxonomy oral session with a
student and scores it. Firebase-authenticated like Auth/Core (same
`shared/auth` seam). Models are fixed in code per pipeline, never
client-selectable - see `services/colearner/README.md`.

| Pipeline / feature | Used by | Models |
| --- | --- | --- |
| **Realtime** (WebRTC, full duplex) | nool-apps AI Assessor (the production voice test path) | `gpt-realtime-2.1-mini` (speech-to-speech), `gpt-4o-mini-transcribe` (transcript), `gpt-4o-mini` (Bloom report) |
| **Chained** (one recorded clip per turn) | Core's legacy WS stream (`school.voice_pipeline`, default `chained`), playground | `whisper-1` STT, `gpt-4o-mini` LLM, Edge-TTS |
| **Multimodal** (audio straight into the LLM) | Core's legacy WS stream, playground | `gemini-3.5-flash-lite`, Edge-TTS |
| **Cascaded** (prototype, cost/latency comparison) | playground only | `gpt-4o-mini-transcribe` STT, `gpt-4.1-mini` LLM, `gpt-4o-mini-tts` TTS |
| **Homework insight** (`POST /api/v1/homework-insight`) | Core, student Homework screen | `gpt-4o-mini` (deterministic fallback paragraph on failure) |

A dev-only browser test page for the pipelines lives at
`/api/v1/colearner/playground`, served only when `PLAYGROUND_ENABLED` is on
(set in `compose.override.yml`, never in production); with it on, requests
without a token are also let through.

### Generation Service

Stateless LLM question writer; only Core calls it, it has no database.
`POST /v1/homework/gap-fill` writes questions for the Homework slots the
question bank couldn't fill: `gpt-5.1` writes, `gpt-5-mini` verifies, failed
slots are regenerated once. See `services/generation/README.md`.

### KG Service

Curriculum and grounded generation over the `nool-kg` textbook graph
(FalkorDB behind nool-kg's REST API, `NOOL_KG_BASE_URL`). Serves the
curriculum tree that the admin catalog sync imports, question paper
generation and replacement candidates, and voice test reference content
(`textbook_context` + `reference_questions`) - all generation with
`gpt-5.1` using Structured Outputs. See `services/kg/README.md`.

### Where each AI step runs

| Step | Path | Model(s) | Fallback |
| --- | --- | --- | --- |
| Voice test conversation | app -> Core -> colearner realtime | `gpt-realtime-2.1-mini` + `gpt-4o-mini-transcribe` | none - the call fails with 503 |
| Voice test scoring (Bloom report) | colearner | `gpt-4o-mini` | - |
| Voice test reference content (on test create, background) | Core -> kg | `gpt-5.1` | session uses colearner's generic defaults |
| Homework questions | Core: question bank -> generation gap-fill -> relaxed bank -> kg -> deterministic generator | `gpt-5.1` + `gpt-5-mini` verify (generation), `gpt-5.1` (kg) | `DeterministicContentGenerator`, last resort only |
| Homework insight | Core -> colearner | `gpt-4o-mini` | fixed placeholder paragraph |
| Question paper questions / replacements | Core -> kg | `gpt-5.1` | none - errors surface to the teacher |
| Teacher Assistant chat | Core | none - keyword-matched canned replies | - |

`services/core/src/services/content_generator.py`'s
`DeterministicContentGenerator` still exists, but only as that last
Homework fallback (chapter never synced from KG, or every real source
failed) - it is not the primary AI anywhere.

### A voice test, end to end

1. Teacher creates a Voice Test (`POST /api/v1/tests`); Core asks kg, in
   the background, for `textbook_context` and `reference_questions` and
   stores them on the test.
2. Student opens it in nool-apps: `POST /api/v1/ai-assessor/sessions`
   creates an `ai_assessor_sessions` row.
3. The app creates a WebRTC peer connection (mic track + data channel) and
   sends its SDP offer to `POST /api/v1/ai-assessor/sessions/{id}/realtime`.
   Core builds the colearner `SessionConfig` (grade, subject, chapter, Bloom
   levels, time limit, grounding) and forwards it with the student's own
   Firebase token to colearner `POST /api/v1/colearner/realtime/calls`.
4. Colearner creates the OpenAI Realtime call with the instructions and
   tools server-side (the app never sees the key or the prompt), returns the
   SDP answer, and attaches a sideband WebSocket to the same call to record
   the transcript itself.
5. Audio flows directly app <-> OpenAI. Server VAD detects turn ends; the
   student can interrupt. The model calls `end_session` after the last
   question (or on `[TIME_UP]`), speaks a closing summary, and colearner
   hangs up after a 30 s grace if the client doesn't.
6. The app calls `POST /api/v1/ai-assessor/sessions/{id}/finish`; Core calls
   colearner's `.../realtime/calls/{call_id}/finish`, which builds the Bloom
   report from the server-side transcript (`gpt-4o-mini`). Core scores it
   and records the test result, XP and the automatic follow-up Homework.

### Known limitations

- **Colearner state is in process memory** (`session_store.py`, the
  realtime call registry) - lost on restart, invisible to a second replica.
  Colearner must run as a **single replica**; Core's own WS fan-out
  (`src/services/realtime.py`) has the same single-instance constraint.
- Colearner reports are written to a container-local `reports/` directory.
- Voice tests and question papers have no non-LLM fallback: no
  `OPENAI_API_KEY` (or kg / nool-kg down) means they fail.

### PostgreSQL

Runs as a Compose service for local development with a persistent named
volume. Production uses managed PostgreSQL. See `database/README.md`.

## Identity & Authorization

Both services verify tokens independently - self-contained JWTs, so
verification doesn't require a network call to the Auth service per
request. The shared verification logic lives in `shared/auth/` since it's
generic infrastructure, not business logic - and it's provider-agnostic on
purpose:

- `shared/auth/provider.py` defines `AuthenticatedUser` (the
  provider-agnostic identity shape every provider must produce) and the
  `IdentityProvider` interface every provider implements.
- `shared/auth/providers/firebase.py` is the only concrete implementation
  today, and the only file that imports `firebase_admin`.
- Each service's `deps.py` (`services/auth/src/services/token_service.py`
  and `services/core/src/api/deps.py`) has exactly one line naming
  `FirebaseIdentityProvider` concretely - mirroring how nool-apps'
  `AuthProvider.tsx` has exactly one line naming its concrete identity
  service. Everything else (routes, business logic, tests using a fake
  provider) depends only on `AuthenticatedUser`/`IdentityProvider`.

Swapping identity providers means: add a class under `shared/auth/providers/`
implementing `IdentityProvider`, and change those two construction call
sites. Provider-specific configuration (env vars, `compose.yml`,
`requirements.txt`) still has to change too - that part can't be abstracted
away by any code design - but no route handler, business logic, or test
using a fake provider needs to.

Conceptually, once identity resolution exists in Core:

```
Firebase UID -> nool User -> Role -> School/Tenant -> Teacher/Student profile
```

Authorization (what a user can access) is enforced server-side and is
distinct from authentication (who the user is). Initial roles: `SUPER_ADMIN`,
`SCHOOL_ADMIN`, `TEACHER`, `STUDENT`. Permissions are implemented only as
features require them.

## Shared infrastructure

`shared/` contains only generic, cross-service infrastructure - never
business logic:

- `shared/config` - base environment-driven settings class.
- `shared/logging` - structured JSON logging, request ID + trace/span
  context/middleware.
- `shared/errors` - the standard error envelope and FastAPI exception handlers.
- `shared/types` - common response models (health/readiness).
- `shared/auth` - provider-agnostic token verification (`IdentityProvider`
  interface + `AuthenticatedUser`); Firebase is the one implementation,
  under `shared/auth/providers/`.
- `shared/tracing` - OpenTelemetry wiring (exported to Jaeger locally) -
  see `docs/OBSERVABILITY.md`.

## Service boundaries

Auth, Core and the AI services are logically independent within one repository (a deliberate
monorepo decision - see the root README). Auth does not import Core business
modules; Core does not import Auth implementation details. A service is
extracted into its own repository only if it later needs independent
deployment, scaling, ownership, runtime, or isolation.

## Error handling

Every error - expected (`AppError` subclasses) or unexpected - renders as:

```json
{ "error": { "code": "VALIDATION_ERROR", "message": "Invalid request.", "request_id": "req_..." } }
```

Stack traces, database errors, internal paths, and secrets are never
exposed to clients.
