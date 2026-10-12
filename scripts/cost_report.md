# AI cost report

`scripts/cost_report.py` sums the AI cost the services measured and logged
themselves. It does not estimate anything and makes no model calls. Output is
per feature and per student, in INR and USD (USD x 86.5, the same indicative
rate the services use).

## Run

```sh
# from nool-core/, against the running stack (reads docker compose logs)
python scripts/cost_report.py
python scripts/cost_report.py --since 2026-10-01 --until 2026-10-31
python scripts/cost_report.py --csv > cost.csv

# or against saved logs (e.g. prod, or a copy taken before a restart)
docker compose logs --no-color generation colearner > ai.log
python scripts/cost_report.py ai.log
```

It needs Python 3.10+ and only the standard library. Days, including the
`--since`/`--until` bounds, are IST calendar days. Container logs are lost when
a container is recreated, so save them first if you want the history.

## What it reads

| Log line | Service | Source |
|---|---|---|
| `generation_call_cost` | generation | one per OpenAI call: every `StructuredLLM` call (gpt-5.1 / gpt-5-mini) and every transcription (gpt-4o-mini-transcribe). Includes tokens (input, cached input, output, reasoning), `usd`, `inr` and `prices_checked`. Prices are in `services/generation/src/services/cost.py`. |
| `grade_answer_cost` | colearner | one per spoken daily-practice answer graded (gpt-5-mini). |
| `regrade_cost` | colearner | one per teacher-triggered voice test regrade. |
| `realtime_call_cost` | colearner | one per live voice test at hang-up: the live call, its transcription and the Bloom report. |
| `realtime_call_started` | colearner | links a realtime call's `call_id` to its student (`owner_ref` is Core's ai_assessor session id). |

Core sets the feature and the requesting user on every call to generation and
colearner as `X-Nool-Feature`, `X-Nool-Student`, `X-Nool-Teacher` and
`X-Nool-School` headers (`services/core/src/services/ai_context.py`).
Features: `homework`, `mcq_check`, `mcq_explain`, `daily_practice_mcq`,
`voice_prep`, `practice_voice_prep`, `practice_transcribe`, `practice_grade`,
`voice_test`, `voice_regrade`, `paper`, `assistant`, `summary`. A generation
call with no feature header is listed under its endpoint path.

## Columns

- `calls` and `total`: every call for the feature.
- `no student`: the part of `total` with no student id. This covers
  teacher-side features (paper, homework, assistant, voice test prep), and
  voice calls from before this logging existed or with no start line.
- `students`, `avg/stu`, `p50/stu`, `p90/stu` and `max/stu`: each student's
  total for the feature in the period. Percentiles are nearest-rank.
- `stu-days` and `/stu/day`: the number of distinct (student, IST day) pairs
  with at least one call, and the student-attributed cost divided by that
  number. This is the cost per active student per day.
- The `ALL` row covers every feature. Its per-student figures add up each
  student's spend across all features.

## Limits

- Shared work is billed to whoever triggered it. For example, the daily
  practice pool or a class's MCQ generation is attributed to the student
  whose request wrote it, even though later students reuse it for free.
- Calls from background jobs that run outside a user request carry only the
  feature.
- kg (gpt-5.1), colearner's homework insight and `/insights` calls, and the
  cascaded prototype do not log a cost line yet, so they are not included.
