# Core Service

Owns the product domain: Schools, Teachers, Students, Classes, Curriculum,
Voice Tests, Test Results, Homework, Journey, Practice Bank, Question Papers,
Dashboards, Assistant, AI Assessor sessions, Leaderboard, and the Super
Admin / School Admin surfaces - the full API contract defined by nool-app's
`spec/docs/api-reference.html` (and the `services/domain/`/`types/domain/`
TypeScript it's generated from).

The live, always-accurate API spec is the running service's own OpenAPI
document - `/docs` (Swagger UI) and `/openapi.json`, both directly and
through NGINX (`/api/docs`, `/api/openapi.json`) - see
`docs/DEVELOPMENT.md`'s "Direct service access" section. This file doesn't
duplicate the endpoint list; it stays accurate because FastAPI generates it
from the actual route/schema code.

## Architecture

- **Models**: `src/domain/models/`, one file per resource group, SQLAlchemy
  2.0 async ORM, `snake_case` tables/columns, UUID primary keys.
- **Schemas**: `src/api/schemas/`, one file per resource group, mirroring
  `types/domain/*.ts` field-for-field. `common.py`'s `CamelModel` base
  converts every request/response body to/from `camelCase` JSON
  automatically - Python code stays `snake_case` (PEP8).
- **Routes**: `src/api/routes/`, one router per resource group, all under
  `/api/v1`. No repository-class-per-resource layer - route handlers query
  the database directly via SQLAlchemy (root `CLAUDE.md` discourages
  unnecessary abstraction); `src/repositories/lookups.py` and
  `roster_repository.py` hold only the handful of scoped "get or 404"
  helpers genuinely shared across multiple routers.
- **Authorization**: `src/api/deps.py`'s `get_current_app_user` resolves a
  verified Firebase token to Core's own `users` row (role, school_id -
  a bare Firebase custom claim can't carry school_id); `require_role(*roles)`
  gates each route. Teacher/School Admin routes are scoped to the caller's
  `school_id`; Student routes (`/me/...`) are scoped to the caller
  themselves; `/admin/...` (Super Admin) is cross-tenant by design.
- **AI-backed content**: Core calls the internal AI services rather than
  any model directly - `colearner_client.py` (AI Assessor realtime voice
  calls: create + finish), `colearner_insight_client.py` (Homework
  insight), `generation_client.py` (voice test question prep, Homework
  gap-fill) and `kg_client.py` (curriculum sync, question papers, voice
  test textbook context, KG-grounded Homework). `voice_test_prep.py` picks
  and preps a test's questions in the background after create.
  `content_generator.py`'s `DeterministicContentGenerator` is
  only the last Homework fallback (`homework_content_resolver.py`). See
  `docs/ARCHITECTURE.md` for the model used at each step.

## Endpoints

| Method | Path      | Description                                   |
| ------ | --------- | ---------------------------------------------- |
| GET    | `/health` | Process health.                                |
| GET    | `/ready`  | Ready when PostgreSQL is reachable.            |

Every other endpoint (69 HTTP routes + 1 WebSocket) is documented live at
`/docs` - see "Architecture" above.

## Configuration

Read from the environment (see `.env.example` at the repo root):

| Variable                   | Required | Description                                   |
| ---------------------------- | -------- | ---------------------------------------------- |
| `SERVICE_NAME`                | no       | Defaults to `core`.                            |
| `ENVIRONMENT`                 | no       | Defaults to `development`.                     |
| `LOG_LEVEL`                   | no       | Defaults to `INFO`.                            |
| `POSTGRES_USER`               | yes      | Database user.                                 |
| `POSTGRES_PASSWORD`           | yes      | Database password.                             |
| `POSTGRES_DB`                 | yes      | Database name.                                 |
| `POSTGRES_HOST`               | no       | Defaults to `postgres` (the Compose service).  |
| `POSTGRES_PORT`               | no       | Defaults to `5432`.                            |
| `FIREBASE_PROJECT_ID`         | no       | Needed for the auth middleware to verify tokens. |
| `FIREBASE_CREDENTIALS_PATH`   | no       | Path to a service account JSON key.            |
| `KG_SERVICE_URL`              | no       | kg service base URL (compose: `http://kg:8000`). |
| `COLEARNER_SERVICE_URL`       | no       | colearner base URL; voice tests 503 without it. |
| `GENERATION_SERVICE_URL`      | no       | generation base URL; Homework skips gap-fill without it. |

## Database & migrations

Connection management lives in `src/repositories/database.py`. Migration
tooling (Alembic) lives in `database/` at the repo root - see
[`database/README.md`](../../database/README.md).

## Local development

```bash
cd services/core
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
POSTGRES_USER=nool POSTGRES_PASSWORD=nool POSTGRES_DB=nool_core POSTGRES_HOST=localhost \
  uvicorn src.main:app --reload
```

## Tests

```bash
cd services/core
pytest
```

Most tests never require a real PostgreSQL instance - database connectivity
is exercised through mocks (see `tests/test_ready.py`). `tests/conftest.py`'s
`db_session` fixture is the one exception: it runs against the actual dev
Postgres (via `localhost:${POSTGRES_PORT_HOST:-5433}`, reading credentials
from the repo root's `.env`) for tests that need to prove real query
behavior - e.g. `tests/test_authorization.py`'s school-scoping tests. It
skips itself cleanly (not a failure) if that Postgres isn't reachable, so
`pytest` still passes standalone; run `docker compose up -d` first to
exercise those tests for real.
