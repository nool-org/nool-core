nool-core — Backend Engineering Instructions

Mission

Build nool-core, the backend platform for noolAI.

This repository contains:

Authentication service

Core business API

NGINX reverse proxy

Database/migrations

Docker-based local development

Shared infrastructure/configuration

nool-app and admin web apps are separate repositories.

Current Goal

Phase 1 (backend foundation) is done: repository structure, Docker infrastructure, Auth/Core skeletons, NGINX, PostgreSQL, health/readiness, configuration, logging/error handling, testing foundation.

Phase 2: build the full product API in Core, against the contract already defined by nool-app's `spec/docs/api-reference.html` (and the `services/domain/`/`types/domain/` TypeScript it's generated from, which is the actual source of truth).

Implement, for every resource group in that contract:

Schools, Teachers, Students, Classes, Curriculum (Subjects/Chapters/Topics), Datasets

Voice Tests, Test Results

Homework

Question Papers

Teacher Dashboard, Assistant

Student Dashboard, Assigned Tests, AI Assessor sessions, Homework/Journey/Practice Bank/Progress/Leaderboard (student-facing)

Super Admin (schools, plans, subscriptions, platform analytics, School Admin management)

School Admin (teacher/student roster, classes, school curriculum, school analytics/subscription)

notifications remain out of scope until a real feature needs them.

AI-backed content is real and lives in three internal services that Core calls (models are fixed in code; see docs/ARCHITECTURE.md for the full table and the voice test flow):

colearner — the AI Assessor voice test: WebRTC realtime voice (gpt-realtime-2.1-mini, transcript gpt-4o-mini-transcribe, Bloom report gpt-5-mini, scores computed in code); per-turn chained (whisper-1, gpt-4o-mini, Edge-TTS) and multimodal (gemini-3.5-flash-lite, Edge-TTS) pipelines; a cascaded prototype (gpt-4o-mini-transcribe, gpt-4.1-mini, gpt-4o-mini-tts); Homework insight (gpt-4o-mini); a dev-only playground at /api/v1/colearner/playground behind PLAYGROUND_ENABLED.

generation — Homework gap-fill questions (gpt-5.1 writes, gpt-5-mini verifies).

kg — curriculum tree, question paper questions and voice test grounding over the nool-kg textbook graph (gpt-5.1).

Core's `DeterministicContentGenerator` (services/core/src/services/content_generator.py) is only the last-resort Homework fallback. Colearner keeps session state in process memory, so it runs as a single replica.

Repository Structure

nool-core/
├── CLAUDE.md
├── README.md
├── .env.example
├── .gitignore
├── compose.yml
├── nginx/
│ ├── nginx.conf
│ └── conf.d/
│ └── default.conf
├── services/
│ ├── auth/
│ │ ├── src/
│ │ │ ├── api/
│ │ │ ├── domain/
│ │ │ ├── services/
│ │ │ ├── repositories/
│ │ │ ├── config/
│ │ │ └── main.py
│ │ ├── tests/
│ │ ├── Dockerfile
│ │ └── README.md
│ └── core/
│ ├── src/
│ │ ├── api/
│ │ ├── domain/
│ │ ├── services/
│ │ ├── repositories/
│ │ ├── config/
│ │ └── main.py
│ ├── tests/
│ ├── Dockerfile
│ └── README.md
├── database/
│ ├── migrations/
│ └── README.md
├── shared/
│ ├── config/
│ ├── logging/
│ ├── errors/
│ └── types/
├── scripts/
└── docs/
├── ARCHITECTURE.md
├── DEVELOPMENT.md
└── DATABASE.md

Adapt the exact language/framework structure if the repository already has a selected stack. Do not add unnecessary abstractions.

Docker

The repository must support:

docker compose up
docker compose up --build
docker compose down
docker compose logs -f
docker compose ps

Initial local stack:

                    NGINX
                      │
              ┌───────┴───────┐
              ▼               ▼
        Auth Service      Core Service
              │               │
              └───────┬───────┘
                      ▼
                  PostgreSQL

PostgreSQL container — required

PostgreSQL MUST run as a real Docker Compose service for local development.

Use the official PostgreSQL image.

Requirements:

persistent named Docker volume

database name from environment configuration

database user from environment configuration

database password from environment configuration

health check using pg_isready

no credentials hardcoded in source code

Auth and Core connect through the Docker Compose network using the hostname postgres

do not use localhost for service-to-PostgreSQL connections inside containers

Expected connection shape:

postgresql://<user>:<password>@postgres:5432/<database>

The database must remain available across:

docker compose down
docker compose up

unless the volume is intentionally removed with:

docker compose down -v

docker compose down -v is destructive for local database data and must be documented clearly.

compose.yml initially contained nginx, auth, core and postgres; it now also runs jaeger (tracing), kg, generation and colearner, each added for an actual requirement. compose.override.yml adds local-only ports and azurite; compose.prod.yml is the production overlay.

Do not add Redis, Kafka, RabbitMQ, Elasticsearch, Kubernetes or other infrastructure without an actual requirement.

Every application container must:

have a health check

use environment variables

avoid hardcoded secrets

expose only required ports

support graceful shutdown

produce useful logs

PostgreSQL uses a persistent Docker volume for local development.

Production Database

Docker PostgreSQL is for local development.

Production should use managed PostgreSQL unless explicitly decided otherwise.

The application must not depend on Docker-specific database behavior.

NGINX

NGINX is the external HTTP entry point.

Initial routing:

/auth/_ → Auth Service
/api/_ → Core Service

NGINX must:

forward request IDs

preserve required headers

support required CORS configuration

enforce reasonable request limits

return clean errors

contain no business logic

Authentication business logic belongs to Auth, not NGINX.

Auth Service

Firebase Authentication is the identity provider.

Architecture:

nool-app
↓
NGINX
↓
Auth Service
↓
Firebase

The backend verifies Firebase ID tokens.

Do NOT implement password authentication.
Do NOT store Firebase passwords.

Eventually:

Firebase UID
↓
nool User
↓
Role
↓
School/Tenant
↓
Teacher/Student profile

Initial Auth foundation only:

Firebase token verification foundation

authenticated-user extraction

health

readiness

configuration

errors

logging

tests

Do not implement complete user management yet.

Core Service

The Core service will eventually contain:

Schools

Teachers

Students

Classes

Curriculum

Tests

Test Results

Homework

Journey

Practice Bank

Question Papers

Datasets

Question Banks

Subscriptions

Analytics

Foundation (done): startup, configuration, health, readiness, request ID, error handling, logging, database connectivity, migration support, authentication middleware foundation.

Now build the product schema and API on top of that foundation — see "Current Goal" above for the full resource list.

Database

Use PostgreSQL.

Create:

connection management

migration tooling

environment configuration

readiness check

transaction support

The full product schema (see "Current Goal") is now in scope. snake_case table and column names throughout. Migrations via Alembic (`database/`), run with `./scripts/migrate.sh`.

Identity

Firebase is the identity provider. PostgreSQL stores application-level identity.

Conceptually:

Firebase
│ firebase_uid
▼
nool User
├── role
├── school_id
└── profile

Never use email as the primary identity key. Use stable internal IDs. Store Firebase UID as an external identity reference.

Authorization

Authentication and authorization are different.

Authentication = who is the user?
Authorization = what can the user access?

Authorization must be enforced server-side. Frontend role checks are never a security boundary.

Initial roles:

SUPER_ADMIN

SCHOOL_ADMIN

TEACHER

STUDENT

Implement permissions only when required by a feature.

API

Use:

/api/v1/

Use clear resource-oriented endpoints.

Examples:

/api/v1/users
/api/v1/classes
/api/v1/tests
/api/v1/homework

Do not create endpoints merely for convenience.

Errors

Use:

{
"error": {
"code": "VALIDATION*ERROR",
"message": "Invalid request.",
"request_id": "req*..."
}
}

Never expose stack traces, database errors, internal paths or secrets.

Request IDs

Every incoming request must receive or propagate a request ID.

Use:

X-Request-ID

The ID must be available in logs and returned to clients when appropriate.

Configuration

Never hardcode:

Firebase credentials

database credentials

API keys

secrets

production URLs

Use environment variables.

Provide .env.example.
Never commit .env.

Logging

Use structured logging with:

timestamp

level

service

request_id

route

status

duration

Never log passwords, Firebase tokens, authorization headers or secrets.

Health

Every service exposes:

/health
/ready

/health checks process health.
/ready checks required dependencies such as PostgreSQL where appropriate.

Testing

Every service must have:

unit tests

API tests

integration tests where appropriate

At minimum test:

startup

health

readiness

configuration failures

database connectivity

authentication failures

authorization failures

valid authentication

Tests must not require production infrastructure.

Service Boundaries

Keep Auth and Core logically independent:

services/
├── auth/
└── core/

Do not import Core business modules into Auth.
Do not import Auth implementation details into Core.

Shared code should contain only genuinely generic infrastructure. Avoid a giant shared business-logic package.

Monorepo Decision

This repository intentionally contains Auth and Core for now.

Do not split them into separate repositories prematurely.

A service can be extracted later if it requires:

independent deployment

independent scaling

independent ownership

different runtime

strong isolation

First Claude Code Task

When first opened:

Read this CLAUDE.md completely.

Inspect the repository.

Do not implement business features.

Create the folder structure.

Create Docker Compose.

Create NGINX.

Create Auth service skeleton.

Create Core service skeleton.

Create PostgreSQL development container.

Create health/readiness endpoints.

Create configuration system.

Create logging foundation.

Create testing foundation.

Create documentation.

Run the complete local stack.

Verify NGINX reaches Auth and Core.

Verify Auth and Core reach PostgreSQL.

Run tests.

Run lint/typecheck.

Report exactly what was created.

Do not continue into business logic.

Definition of Done

Repository structure exists

Docker Compose works

NGINX starts

Auth starts

Core starts

PostgreSQL starts

Auth health works

Core health works

Readiness works

NGINX routes /auth/\*

NGINX routes /api/\*

Services connect to PostgreSQL

Environment variables work

No secrets committed

Logging works

Request IDs work

Error handling works

Tests pass

Lint/typecheck passes

docker compose up --build works from a clean checkout

Documentation is complete

Do not declare completion until these checks have actually been run.
