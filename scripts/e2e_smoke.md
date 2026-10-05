# e2e smoke test

`scripts/e2e_smoke.py` checks a release against the **running** dev stack. It calls the real APIs through nginx (`http://nginx:80`) as a teacher and a student. It only reads the database, to pick a chapter and to watch `prep_status`.

## Run

From the repo root (Git Bash), with the stack up:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network nool-core_nool_network \
  --env-file .env \
  -e FIREBASE_CREDENTIALS_PATH=/run/fb.json \
  -v "$PWD/secrets/nool-test-app-firebase.json:/run/fb.json:ro" \
  -v "$PWD/scripts:/s:ro" \
  -e TEACHER_EMAIL=sudha@nool.dev -e STUDENT_EMAIL=manish@nool.dev \
  nool-core-core python /s/e2e_smoke.py
```

Each step prints `PASS`/`FAIL`/`SKIP`, its time in seconds and a detail. A summary follows. The script exits with `1` if any step fails.

Steps:

1. nginx `/health`.
2. Tokens and `GET /api/v1/me` for both users.
3. The teacher's Class 10 section that contains the student.
4. A chapter and topic with dataset questions for all 5 levels, picked by a read-only SQL query.
5. `POST /tests`, targeting only that student.
6. `POST /tests/{id}/schedule`.
7. Wait until `prep_status` is no longer `PENDING`, then require `READY`. This step makes a live generation call (about $0.05 to $0.10).
8. `GET /tests/{id}/questions`: one dataset question per Bloom level, reworded, with rubric points.
9. The test shows in `GET /me/assigned-tests`.
10. `POST /ai-assessor/sessions` returns `questionCount == 5`.
11. `GET /me/dashboard`.
12. `GET /me/tests/{id}/insights` returns 404, because there is no result yet.
13. `GET /me/mcq-tests`.
14. `GET /tests/{id}/reviews`.

If a setup step (1 to 7) fails, the remaining steps are skipped. If a check step (8 to 14) fails, the script records it and carries on.

The voice part stops after the session opens. Core's `/finish` gets the C3 payload from colearner for a live realtime call, and there is no test hook to inject a synthetic payload. So the steps after a finish (result, insights, MCQ unlock, XP) are not covered.

## Tokens

There are no documented test passwords. For each of `TEACHER` and `STUDENT`, the script takes the first of these that is set:

- `<ROLE>_TOKEN`: a Firebase ID token you already have. For example, copy it from the app's network tab or get it from `POST /auth/api/v1/login`.
- `<ROLE>_EMAIL`: the script looks up that Firebase user with the Admin SDK (the service-account file mounted above), mints a custom token and exchanges it at `signInWithCustomToken` with `FIREBASE_WEB_API_KEY` from `.env`. No password is involved, and tokens are never printed.

The dev seed pair is `sudha@nool.dev` (TEACHER, Class 10A Science) and `manish@nool.dev` (STUDENT, 10A).

## Optional env

| Variable | Default | Purpose |
|---|---|---|
| `BASE_URL` | `http://nginx:80` | Where the APIs are reached |
| `CHAPTER_ID` + `TOPIC_ID` | auto | Skip the chapter pick |
| `PREP_TIMEOUT` | `240` | Seconds to wait for prep |

## What it creates and how to clean up

Each run creates:

- one voice test (DRAFT, then SCHEDULED) assigned to the student, with its questions, Bloom levels and target row;
- one AI assessor session with `context_label = 'SMOKE e2e'`.

Voice tests have no name field, so the "SMOKE " tag goes on the session. The script also prints `created: voice test <id>`.

Smoke tests stay in the student's assigned list until they are deleted. Clean up manually on the dev DB only:

```sql
begin;
create temp table smoke_tests as
  select distinct test_id id from ai_assessor_sessions where context_label = 'SMOKE e2e' and test_id is not null;
-- also add ids from "created: voice test <id>" of runs that failed before opening a session:
-- insert into smoke_tests values ('<id>');
delete from ai_assessor_session_bloom_levels where session_id in (select id from ai_assessor_sessions where test_id in (select id from smoke_tests));
delete from ai_assessor_session_records     where session_id in (select id from ai_assessor_sessions where test_id in (select id from smoke_tests));
delete from ai_assessor_sessions        where test_id in (select id from smoke_tests);
delete from voice_test_questions        where voice_test_id in (select id from smoke_tests);
delete from voice_test_bloom_levels     where test_id in (select id from smoke_tests);
delete from voice_test_target_students  where test_id in (select id from smoke_tests);
delete from voice_tests                 where id in (select id from smoke_tests);
commit;
```

None of these foreign keys cascade. A smoke run never finishes a session, so it leaves no `student_test_results`, `mcq_tests` or `homework` rows. If one of these deletes fails on a foreign key, someone took the test by hand, and you need to delete those rows first.
