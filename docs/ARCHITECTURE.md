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
                 Auth      Core ---> Colearner ---> OpenAI (Realtime, transcribe, gpt-5-mini grader)
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

The voice co-learner ("Milo"): runs a Bloom's Taxonomy oral test with a
student and grades it. Firebase-authenticated like Auth/Core (same
`shared/auth` seam). Models are fixed in code, never client-selectable -
see `services/colearner/README.md`. Realtime is the only production voice
path; the chained and multimodal pipelines (and Gemini) were removed in
wave 2.

| Pipeline / feature | Used by | Models |
| --- | --- | --- |
| **Realtime** (WebRTC, full duplex) | nool-apps AI Assessor - the production voice test | `gpt-realtime-2.1-mini` (speech-to-speech, voice `marin`), `gpt-4o-mini-transcribe` (server-side transcript) |
| **Grader** (`src/services/evaluation/grader.py`) | realtime `.../finish` (and cascaded) | `gpt-5-mini`, strict JSON schema, per-question rubric; every aggregate computed in code |
| **Cascaded** (prototype, cost/latency comparison) | dev playground only | `gpt-4o-mini-transcribe` STT, `gpt-4.1-mini` LLM, `gpt-4o-mini-tts` TTS |
| **Homework insight** (`POST /api/v1/homework-insight`) | Core, student Homework screen | `gpt-4o-mini` (Core shows a placeholder paragraph on failure) |

A dev-only browser page at `/api/v1/colearner/playground` drives the
Realtime and Cascaded pipelines without the app. It is served only when
`PLAYGROUND_ENABLED` is on (set in `compose.override.yml`, never in
production); with it on, requests without a token are also let through.
Real model calls are made and billed.

**Grader accuracy gate** - `services/colearner/evals/`: a labelled set of
transcripts (`grader_set.json`) and `grader_eval.py`, which grades them with
the real grader and fails (exit 1) if verdict accuracy is under 95% or more
than 1 in 30 wrong answers are marked correct. Run it after any change to
the grader prompt, schema or model - locally (see `evals/README.md`, about
$0.09 per run) or via the manual "Grader eval" GitHub Actions workflow. It
is not part of the normal test suite (it spends API credit).

### Generation Service

Stateless LLM writer; only Core calls it, it has no database. See
`services/generation/README.md`.

- `POST /v1/voice-test/prep` - rewords the dataset questions Core picked
  for a voice test into simple spoken Indian classroom English and writes
  each one's rubric (`expected_points`, `misconceptions`, two `hints`):
  `gpt-5.1` rewords, code checks plus `gpt-5-mini` verify, one regenerate,
  then the original wording.
- `POST /v1/homework/gap-fill` - writes questions for the Homework slots the
  question bank couldn't fill: `gpt-5.1` writes, `gpt-5-mini` verifies,
  failed slots are regenerated once.

### KG Service

Curriculum and grounded generation over the `nool-kg` textbook graph
(FalkorDB behind nool-kg's REST API, `NOOL_KG_BASE_URL`). Serves the
curriculum tree that the admin catalog sync imports, question paper
generation and replacement candidates, and a voice test's
`textbook_context` (grounding text only - voice test questions come from
datasets, `reference_questions` is always `[]`). Generation uses `gpt-5.1`
with Structured Outputs. See `services/kg/README.md`.

### Where each AI step runs

| Step | Path | Model(s) | Fallback |
| --- | --- | --- | --- |
| Voice test textbook context (on test create, background) | Core -> kg | `gpt-5.1` | session runs ungrounded (generic syllabus instruction) |
| Voice test questions (on test create, background) | Core picks one dataset question per Bloom level -> generation `/v1/voice-test/prep` | `gpt-5.1` reword + `gpt-5-mini` verify | dataset text as-is, no rubric; no question found -> Milo improvises from the textbook context |
| Voice test conversation | app -> Core -> colearner realtime | `gpt-realtime-2.1-mini` + `gpt-4o-mini-transcribe` | none - the call fails with 503 |
| Voice test grading | colearner, on `.../finish` | `gpt-5-mini` (rubric per question; scores computed in code) | none - the session is stored with `needs_review` and no score |
| Homework questions | Core: question bank -> generation gap-fill -> relaxed bank -> kg -> deterministic generator | `gpt-5.1` + `gpt-5-mini` verify (generation), `gpt-5.1` (kg) | `DeterministicContentGenerator`, last resort only |
| Homework insight | Core -> colearner | `gpt-4o-mini` | fixed placeholder paragraph |
| Question paper questions / replacements | Core -> kg | `gpt-5.1` | none - errors surface to the teacher |
| Teacher Assistant chat | Core | none - keyword-matched canned replies | - |

`services/core/src/services/content_generator.py`'s
`DeterministicContentGenerator` still exists, but only as that last
Homework fallback (chapter never synced from KG, or every real source
failed) - it is not the primary AI anywhere.

### A voice test, end to end

1. **Create** - the teacher creates a Voice Test (`POST /api/v1/tests`,
   DRAFT) for a chapter (optionally a topic) and a set of Bloom levels, then
   schedules it (`POST /tests/{id}/schedule`).
2. **Prep** (background task after create, `services/voice_test_prep.py`) -
   Core asks kg for the chapter's `textbook_context`, then picks one
   question per Bloom level of the test (normally 5) from the QA datasets
   the teacher may use, text-only (MCQ/true-false only if nothing else fits
   a level), and sends them to generation `/v1/voice-test/prep`. Each
   becomes a `voice_test_questions` row: order, Bloom level, topic/subtopic,
   concepts, original/reworded/spoken text, expected points,
   misconceptions, hints. Every student of the test gets the same questions.
3. **Review** (optional) - the teacher sees them in nool-apps
   (`GET /tests/{id}/questions`), can edit or approve one
   (`PATCH .../questions/{qid}`) or swap it for another dataset question of
   the same level (`POST .../swap`). Questions freeze once any student has
   completed the test.
4. **Live session** - the student opens the test:
   `POST /api/v1/ai-assessor/sessions` creates an `ai_assessor_sessions`
   row; the app creates a WebRTC peer connection (mic + data channel) and
   sends its SDP offer to `POST /ai-assessor/sessions/{id}/realtime`. Core
   builds the colearner `SessionConfig` (grade, subject, chapter, time
   limit, `textbook_context` and the test's `questions`) and forwards it
   with the student's own Firebase token to colearner
   `POST /api/v1/colearner/realtime/calls`. Colearner creates the OpenAI
   Realtime call with instructions and tools server-side (the app never
   sees the key or prompt), returns the SDP answer, and attaches a sideband
   WebSocket to record the transcript itself. Audio flows app <-> OpenAI
   directly; server VAD detects turn ends and the student can interrupt.
   Milo asks exactly the prepared questions, in order, through tools -
   `ask_question`, `give_hint` (max 2 per question, from the rubric),
   `give_explanation`, `end_session` - so colearner knows which question
   each student turn answers (first answer vs. follow-up / hint request).
   After `end_session` or the time limit the model gets a 30 s grace, then
   colearner hangs up.
5. **Grading** - the app calls `POST /ai-assessor/sessions/{id}/finish`;
   Core calls colearner `.../realtime/calls/{call_id}/finish`, which grades
   the server-side transcript against each question's rubric with
   `gpt-5-mini` and returns the report (per answer: score, points covered,
   misconception, reason, student response, hints used), the answers and a
   question-tagged transcript. A grading failure comes back as
   `grading_error`.
6. **Results** - Core stores the payload in `ai_assessor_session_records`
   and records `StudentTestResult` (mastery), per-Bloom
   (`StudentTestResultBloomScore`) and per-topic
   (`student_test_result_topic_scores`) scores, XP on the first completion,
   and schedules the automatic follow-up Homework (`homework_autogen.py`).
   On `grading_error` nothing is scored: the record is flagged
   `needs_review`, the attempt counts (no retake) and the teacher sees it
   at `GET /tests/{id}/reviews`.

### Known limitations

- **Colearner call state is in process memory** (`session_store.py`, the
  realtime call registry in `realtime_service.py`) - lost on restart,
  invisible to a second replica. Colearner must run as a **single
  replica**; Core's own WS fan-out (`src/services/realtime.py`) has the
  same single-instance constraint.
- **Prep race**: a session started before the background prep finished
  (~10-60 s after create) gets no questions and Milo improvises from the
  textbook context (`ai_assessor.py`'s `_colearner_config`). Handled by
  work item W3-09.
- Colearner transcript/report files are written to a container-local `reports/`
  directory.
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
