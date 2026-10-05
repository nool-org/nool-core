# nool-core

Backend platform for noolAI, an AI voice co-learner for Indian school
students: authentication service, core business API, AI services
(colearner voice, generation, kg), NGINX reverse proxy, PostgreSQL, and
Docker-based local development.

`nool-apps` and the admin web apps are separate repositories.

## Current status

Backend foundation (Phase 1) is done - repository structure, Docker
infrastructure, service skeletons, health checks, configuration, logging,
error handling, testing foundation. Phase 2 is in progress: the full
product API in Core - Schools, Teachers, Students, Classes, Curriculum,
Voice Tests, Test Results, Homework, Journey, Practice Bank, Question Papers,
Dashboards, Assistant, AI Assessor sessions, Leaderboard, Super Admin, and
School Admin. The AI features are real (OpenAI Realtime voice tests,
LLM-generated Homework and question papers grounded in the nool-kg
textbook graph) - see `docs/ARCHITECTURE.md` for which model runs at which
step, the end-to-end voice test flow and known limitations, and `CLAUDE.md`
for the scope and working rules.

## Repository structure

```
nool-core/
├── CLAUDE.md
├── compose.yml
├── nginx/                 # reverse proxy: routes /auth/* and /api/*
├── services/
│   ├── auth/               # Firebase token verification service
│   ├── core/               # core business API
│   ├── colearner/          # voice co-learner (OpenAI Realtime + gpt-5-mini grader)
│   ├── generation/         # voice test question prep, Homework gap-fill
│   └── kg/                 # curriculum + grounded generation over nool-kg
├── database/               # Alembic migration tooling
├── shared/                 # generic cross-service infrastructure
├── scripts/
└── docs/
```

## Quick start

```bash
cp .env.example .env
docker compose up --build
curl http://localhost:8080/health
```

Set `OPENAI_API_KEY` in `.env` for the AI features. If
the hosted textbook graph is down, run nool-kg locally on port 8010 - see
"The textbook graph (nool-kg) and the local fallback" in
`docs/DEVELOPMENT.md`, which also covers running tests (with fake model
clients, no API keys) and the production deploy (`compose.prod.yml`).

See `docs/DEVELOPMENT.md` for the full local development workflow,
`docs/ARCHITECTURE.md` for system design, `docs/DATABASE.md` for the
database setup and migration workflow, and `docs/OBSERVABILITY.md` for
distributed tracing (Jaeger) and the authentication audit trail.

## Services

| Service | Description | README |
| ------- | ----------- | ------ |
| Auth    | Firebase ID token verification | [`services/auth/README.md`](services/auth/README.md) |
| Core    | Core business API              | [`services/core/README.md`](services/core/README.md) |
| Colearner | AI Assessor voice sessions, Homework insight | [`services/colearner/README.md`](services/colearner/README.md) |
| Generation | Voice test question prep, Homework gap-fill (gpt-5.1 + gpt-5-mini verify) | [`services/generation/README.md`](services/generation/README.md) |
| KG      | Curriculum, question papers, voice-test textbook context over nool-kg (gpt-5.1) | [`services/kg/README.md`](services/kg/README.md) |
