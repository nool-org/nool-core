# Development

## Prerequisites

- Docker and Docker Compose v2
- Python 3.12+ (only needed for running a service outside Docker)

## First-time setup

```bash
cp .env.example .env
# edit .env - at minimum set POSTGRES_PASSWORD to something non-default
```

The AI features also need `OPENAI_API_KEY` (colearner, generation, kg; the
only model key - there is no Gemini key any more), plus the Firebase
credentials - see `.env.example`. Without the keys the stack still starts,
but `/ready` on those services reports 503 and voice tests / question paper
generation fail.

## Running the stack

```bash
docker compose up --build   # build images and start everything
docker compose up           # start without rebuilding
docker compose ps           # see service status/health
docker compose logs -f      # tail logs from all services
docker compose logs -f core # tail logs from one service
docker compose down         # stop and remove containers (data persists)
```

The stack is reachable through NGINX at `http://localhost:8080` (or
whatever `NGINX_PORT` is set to):

```bash
curl http://localhost:8080/health
curl http://localhost:8080/auth/health
curl http://localhost:8080/auth/ready
curl http://localhost:8080/api/health
curl http://localhost:8080/api/ready
```

Auth, Core, and PostgreSQL are not published to the host in `compose.yml`
(production-shaped) - only NGINX is. `compose.override.yml` is auto-loaded
alongside it for local dev and additionally publishes:

- PostgreSQL on `localhost:5433` (override with `POSTGRES_PORT_HOST`) so you
  can point a GUI client (TablePlus, DBeaver, pgAdmin, ...) or `psql`
  directly at it:

  ```
  Host:     localhost
  Port:     5433
  User:     <POSTGRES_USER from .env>
  Password: <POSTGRES_PASSWORD from .env>
  Database: <POSTGRES_DB from .env>
  ```

  ```bash
  psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@localhost:5433/$POSTGRES_DB"
  ```

- Auth on `localhost:8001` and Core on `localhost:8002` (override with
  `AUTH_PORT_HOST`/`CORE_PORT_HOST`), each the exact same app NGINX proxies
  to - useful for hitting a service directly, or for its OpenAPI/Swagger UI:

  ```
  http://localhost:8001/docs        # Auth - interactive Swagger UI
  http://localhost:8001/openapi.json
  http://localhost:8002/docs        # Core - interactive Swagger UI
  http://localhost:8002/openapi.json
  ```

  The same docs are also reachable through NGINX, at
  `http://localhost:8080/auth/docs` and `http://localhost:8080/api/docs` -
  both routes hit the same FastAPI app, so pick whichever is convenient.

- KG on `localhost:8003`, Colearner on `localhost:8004`, Generation on
  `localhost:8005` (`KG_PORT_HOST`/`COLEARNER_PORT_HOST`/`GENERATION_PORT_HOST`),
  each with its own `/docs`.
- Azurite (local Azure Blob stand-in for question images) on `localhost:10000`.

`compose.override.yml` also sets `PLAYGROUND_ENABLED=true` on colearner, so
the browser test page for the voice pipelines (Realtime, and the Cascaded
prototype) is at
`http://localhost:8004/api/v1/colearner/playground` (or via NGINX at
`http://localhost:8080/api/v1/colearner/playground`). It skips sign-in and
spends real model API credit; it must never be enabled in production.

Delete or ignore `compose.override.yml` for a production-shaped deploy -
services stay internal-only without it.

### The textbook graph (nool-kg) and the local fallback

The `kg` service reads curriculum content from nool-kg's REST API at
`NOOL_KG_BASE_URL` (default: the hosted `https://nool-data.draco-labs.in`).
If the hosted graph is down, run nool-kg locally against a local FalkorDB
and point kg at it:

```powershell
# from the nool-kg checkout, with its FalkorDB container on host port 6380
$env:FALKORDB_HOST="localhost"; $env:FALKORDB_PORT="6380"
uv run uvicorn app.main:app --host 0.0.0.0 --port 8010
```

then set `NOOL_KG_BASE_URL=http://host.docker.internal:8010` in `.env` and
recreate kg (`docker compose up -d kg`). If question paper generation or
a voice test's textbook context starts failing with kg 500s, first check that
port 8010 is up. Remove the line again once the hosted graph is back.

### A voice test locally

1. Sync a chapter from KG in the Super Admin catalog (so it has real
   content to ground against), create a class, teacher and student, and
   make sure the school can see a QA dataset with questions for that
   chapter (the voice test's questions are picked from it).
2. As the teacher, create and schedule a Voice Test for that chapter.
   Give the background prep ~10-60 s (Core logs `voice_test_prep_*`
   warnings if something falls back), then check or edit the questions
   on the app's question review screen (`GET /api/v1/tests/{id}/questions`).
3. As the student in nool-apps (web or device), open the assigned test:
   the app negotiates WebRTC through Core -> colearner -> OpenAI Realtime
   and Milo asks those questions; finishing grades the answers and
   records the result, per-topic scores and the auto Homework.

To try the voice pipelines without the app or test data, use the
playground page above (it does not use prepared questions - Milo
improvises from the textbook context you paste). The full flow is described in
`docs/ARCHITECTURE.md` ("A voice test, end to end").

### Destructive: resetting the database

```bash
docker compose down -v   # also deletes the postgres_data volume
```

This permanently deletes all local database data. Do not run it unless you
intend to start from an empty database.

## Running a service outside Docker

Each service can run standalone against your own PostgreSQL/Firebase
config - see `services/auth/README.md` and `services/core/README.md` for
exact commands. Both need `shared/` importable, which their `pyproject.toml`
handles via `pythonpath` when using `pytest`, and via `PYTHONPATH` when
running `uvicorn` (set it to the repo root).

## Migrations

See `database/README.md` and `scripts/migrate.sh`.

## Tests

```bash
cd services/<auth|core|colearner|generation> && pip install -r requirements-dev.txt && pytest
cd services/kg && pip install -r requirements.txt pytest && pytest
```

No test requires a real Firebase or model API - dependencies are mocked at
the boundary (see each service's `tests/`), and the LLM/STT/TTS clients are
replaced with fakes. Core's DB tests run against the dev Postgres when they
can reach it (inside the Compose network, with real `POSTGRES_*`
credentials), creating their own data, and skip otherwise. Run tests **without** model API keys
in the environment so an unfaked call fails instead of spending credit.

To run a suite with exactly the production dependencies, run it inside that
service's already-built image instead of a local virtualenv: `docker run
--rm` the `nool-core-<service>` image on the Compose network, bind-mount the
checkout's `shared/`, `services/<service>/src` and `services/<service>/tests`
over `/app`, pass only the non-secret env it needs (Postgres, Firebase
project, `ENVIRONMENT`) and run `python -m pytest`. This works for any
checkout (e.g. a git worktree) without touching the running containers.

The grader accuracy gate (`services/colearner/evals`, real API, about
$0.09 a run) is separate from these tests - run it after changing the
grader prompt, schema or model; see `services/colearner/evals/README.md`.

## Deploying (production)

Production uses `compose.prod.yml` instead of `compose.override.yml`:
Caddy (automatic Let's Encrypt TLS, `caddy/Caddyfile`) is the only public
listener and proxies to nginx; migrations run as the one-shot `migrate`
service before core starts; the `backup` service takes a nightly
`pg_dump`. Every `.env` key is in `.env.production.example`.

The full server runbook (Hostinger VPS, firewall, DNS, Firebase production
project, first Super Admin, backups, updates/rollback, monitoring and the
pre-launch checklist) is `docs/DEPLOY_HOSTINGER.md`.

Keep colearner at **one replica** (in-memory session state);
`ENVIRONMENT=production` disables the playground regardless of
`PLAYGROUND_ENABLED`. The pilot runs PostgreSQL in Docker on the server;
moving to managed PostgreSQL later is a config change (see the runbook's
"Later: managed PostgreSQL").

## Linting / type-checking

Not yet configured - add per-service tooling (e.g. `ruff`, `mypy`) when the
project is ready to enforce it; keep configuration in each service's
`pyproject.toml` rather than a shared one, matching the service-boundary
principle in `docs/ARCHITECTURE.md`.
