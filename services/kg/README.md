# kg service

Curriculum and grounded question generation over the nool-kg textbook
graph (FalkorDB behind nool-kg's own REST API). Only `core` calls it
(`services/core/src/services/kg_client.py`); it is not routed through NGINX
and has no database of its own.

## What it does

| Method | Path | |
|---|---|---|
| GET | `/curriculum/subjects`, `/curriculum/tree` | Subjects and the Chapter -> Topic tree the Super Admin catalog sync imports. |
| POST | `/question-papers/generate` | Question paper questions (also KG-grounded Homework). |
| POST | `/question-papers/replacement-candidates` | Alternatives for one question. |
| POST | `/voice-tests/generate` | `textbook_context` + `reference_questions` that ground a voice test's colearner session. |
| GET | `/health`, `/ready` | `/ready` checks that nool-kg answers. |

Retrieval (`src/services/nool_kg_client.py`) builds context from each
chapter/topic's `text_content`; generation (`src/services/generator.py`) is
one `gpt-5.1` call with a strict JSON schema (Structured Outputs).

## Config

| Env | Default | |
|---|---|---|
| `NOOL_KG_BASE_URL` | `https://nool-data.draco-labs.in` | nool-kg API. Point at a local nool-kg (`http://host.docker.internal:8010`) when the hosted one is down - see `docs/DEVELOPMENT.md`. |
| `OPENAI_API_KEY` | – | needed for every generate endpoint |

## Run / test

```bash
docker compose up -d kg                  # http://localhost:8003/docs
cd services/kg && pip install -r requirements.txt pytest && pytest
```
