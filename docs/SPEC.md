# nool Platform — Technical Specification

**Status**: reflects the platform as built and verified live, as of 2026-08-25.
**Scope**: `nool-core` (Auth, Core, Colearner, Generation and KG services, PostgreSQL, NGINX), `nool-apps` (React Native/Expo mobile app for Teachers and Students), `nool-school-admin` and `nool-super-admin` (Next.js admin consoles).

---

## 1. Mission & Architecture

nool is a school voice-testing and curriculum platform: an AI voice co-learner for Indian school students. Firebase Authentication is the identity provider; PostgreSQL is the system of record for everything else (schools, users, curriculum, tests, subscriptions).

```
                              NGINX (:8080)
                    /auth/*  │        │  /api/*      │ /api/v1/colearner/*
                     (auth service)  (core service) ─┼─> (colearner service)
                              │        │  ├─> generation service
                              └────┬───┘  └─> kg service ─> nool-kg graph
                                   ▼
                              PostgreSQL
```

Four client apps talk to this stack:

| App | Runtime | Talks to |
|---|---|---|
| `nool-apps` | Expo / React Native (Teacher, Student) | Auth service (login), Core service (everything else) |
| `nool-school-admin` | Next.js, port 3100 | Same |
| `nool-super-admin` | Next.js, port 3101 | Same |
| Auth service | FastAPI | Firebase (identity provider) |
| Core service | FastAPI | PostgreSQL, Firebase Admin SDK (user provisioning) |

**Service boundary**: Auth and Core are logically independent — Auth never imports Core business modules and vice versa. Auth owns all Firebase interaction (token verification, password-check proxying, user provisioning); Core owns the product schema and business logic, and calls the Firebase Admin SDK directly only for account provisioning (creating/resetting users), sharing the same service-account credentials Auth uses.

**AI-backed content** is real and runs in three internal services (models fixed in code; full table and the end-to-end voice flow in `docs/ARCHITECTURE.md`):
- **colearner** — the AI Assessor voice test over WebRTC with OpenAI Realtime (`gpt-realtime-2.1-mini`, transcript by `gpt-4o-mini-transcribe`, Bloom report by `gpt-4o-mini`); older per-turn pipelines chained (`whisper-1` → `gpt-4o-mini` → Edge-TTS) and multimodal (`gemini-3.5-flash-lite` → Edge-TTS); a cascaded prototype (`gpt-4o-mini-transcribe` → `gpt-4.1-mini` → `gpt-4o-mini-tts`); Homework insight (`gpt-4o-mini`); a dev-only playground at `/api/v1/colearner/playground` behind `PLAYGROUND_ENABLED`.
- **generation** — Homework gap-fill (`gpt-5.1` writes, `gpt-5-mini` verifies).
- **kg** — curriculum tree, question paper questions, voice test reference content, grounded in the nool-kg textbook graph (`gpt-5.1`).

`services/core/src/services/content_generator.py`'s `DeterministicContentGenerator` survives only as Homework's last fallback (chapter not synced from KG, or every real source failed).

**Notifications** are explicitly out of scope platform-wide until a real feature needs them (no email, no push, anywhere).

---

## 2. Authentication Architecture

The login request is backend-mediated end-to-end — no client ever sends a password directly to Firebase.

```
1. Client → POST /auth/api/v1/login {email, password}
2. Auth service → Firebase REST accounts:signInWithPassword (server-side, using a
   backend-only Firebase Web API key — never shipped to any client)
3. Auth service → Firebase Admin SDK: create_custom_token(uid)
4. Auth service → Client: {customToken}
5. Client → Firebase client SDK: signInWithCustomToken(customToken)
   → real Firebase session established (ID token, refresh token, persistence,
     onAuthStateChanged — all still handled by Firebase's own client SDK from
     this point on; only the initial credential check moved server-side)
6. Every subsequent API call carries the Firebase ID token; Core/Auth verify it
   via require_role / get_current_user (shared/auth's IdentityProvider protocol).
```

Bad credentials return a clean `401` with no distinction between "wrong password" and "no such account" (preserves Firebase's own email-enumeration protection).

**No email-based account activation exists anywhere.** Every user (Teacher, Student, School Admin) is provisioned by an admin action (invite/create), which:
1. Generates a random temp password server-side (`secrets.token_urlsafe`).
2. Creates the real Firebase Auth account via the Admin SDK (or resets the password on an existing one).
3. Sets a `mustChangePassword: true` Firebase custom claim **and** a `users.must_change_password` DB column (two channels, one fact — `nool-apps` reads the claim via Auth's claims-only `/me`; the admin apps read the DB column via Core's `/me`, since Auth has no DB access by design).
4. Returns the temp password **once**, in the API response, for the admin UI to show and never persist.

The user changes their password on first login (`/change-password` in every app), which clears both the claim and the DB flag via `POST /me/acknowledge-password-change`.

---

## 3. Data Model

~40 tables, `snake_case` throughout, managed via Alembic (`database/migrations/`, run with `./scripts/migrate.sh`).

| Table | Key columns | Notes |
|---|---|---|
| `plans` | name (unique), price_label, teacher_limit, student_limit, test_limit?, question_paper_limit?, active | Catalog. Null `test_limit`/`question_paper_limit` = unlimited. |
| `schools` | name, board, city, contact_email, address?, contact_phone?, principal_name?, status, plan_id? | `plan_id` is **deprecated** — never read for enforcement. |
| `subscriptions` | school_id (unique FK), plan_id (FK), status, renews_at | 1:1 with school. **Sole source of truth** for a school's current plan (via `subscription_repository.get_active_plan`). A school with no row has every limit unenforced, by design. |
| `users` | firebase_uid?, email (unique), display_name, role, school_id?, status, phone_number?, employee_id?, must_change_password | Never keyed by email for identity — `firebase_uid` is the external identity reference. |
| `audit_logs` | actor_id (FK users), action, target_type, target_id, detail? | Written on every mutating Super Admin **and** School Admin action. |
| `subjects` / `chapters` / `topics` | name, chapter→subject FK, topic→chapter FK | **Global catalog, no `school_id`.** Super Admin owns CRUD. |
| `school_curriculum` | school_id, subject_id, enabled | Per-school enable/disable join. School Admin owns this. |
| `datasets` | name, question_count, description | **Global catalog.** |
| `school_datasets` | school_id, dataset_id, enabled | Per-school join. |
| `classes` | school_id, grade, section | — |
| `teacher_class_assignments` | teacher_id, class_id, subject_id | The "assign a class to a teacher" relationship. |
| `student_profiles` | user_id (PK/FK), class_id, roll_number, guardian_name?, guardian_phone?, date_of_birth? | — |
| `voice_tests` (+ bloom_levels, target_students) | class_id, subject_id, chapter_id, topic_id?, duration_minutes, status | — |
| `student_test_results` (+ bloom_scores) | test_id, student_id, mastery_percent | — |
| `homework` (+ datasets, bloom_distribution, target_students, questions; `student_homework_progress`) | test_id, class_id, gap_topic, difficulty, status | — |
| `practice_bank_entries` | student_id, source_homework_id, source_question_id, subject_id?, chapter_id?, topic_id?, bloom_level, text, answer | A standalone copy, not a live pointer to the source question — archived once a Homework's Q&A is confirmed complete. |
| `student_chapter_progress` | student_id, chapter_id, stars, completed_at? | Drives the Journey chapter map; written only by `record_test_completion`, never re-run. |
| `question_papers` (+ dataset_shares, chapters, topics, sections, bloom_distribution, difficulty_distribution, validation, questions) | school_id, created_by (FK users), name, exam_type, board, grade, status | — |
| `student_points` | student_id (PK), points | Leaderboard basis. |
| `assistant_messages` | teacher_id, role, text | Teacher-only chat. |
| `ai_assessor_sessions` (+ bloom_levels) | student_id, test_id?, context_label, duration_seconds | — |

---

## 4. API Reference

All business endpoints require a Firebase ID token (`Authorization: Bearer <token>`) unless noted. Role checks are enforced server-side via `require_role` — client-side role gating is UX only, never the security boundary.

### 4.1 Auth service (`/auth/api/v1/...`)

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/login` | none | Backend-mediated credential check → `{customToken}`. See §2. |
| GET | `/me` | authenticated | Claims-only: `{uid, email, role}`. |
| GET | `/health`, `/ready` | none | Process/dependency health. |

### 4.2 Core service (`/api/v1/...`)

**Profile / session**
- `GET /me` — DB-backed profile incl. `mustChangePassword`.
- `POST /me/acknowledge-password-change` — clears the forced-change flag both places.

**Super Admin — Schools, Plans, Subscriptions**
- `GET/POST /admin/schools`, `GET /admin/schools/{id}`, `PATCH /admin/schools/{id}/status`, `PATCH /admin/schools/{id}` (profile edit)
- `GET/PUT /admin/schools/{id}/default-bloom-distribution`
- `GET/POST/PATCH /admin/plans[/{id}]` — full CRUD
- `GET/POST/PUT /admin/schools/{id}/subscription`
- `GET /admin/analytics/platform` — real computed metrics (mastery, completion, Bloom breakdown), not stubbed; `improvementPercent` is always `0` — no real data source since Retest was removed (see `school_analytics.py`)

**Super Admin — School Admins & Catalog**
- `GET/POST /admin/school-admins`, `POST /admin/school-admins/invite`, `PATCH .../status`, `PATCH .../{id}` (edit), `POST .../{id}/reset-password`
- `GET /admin/audit-log`
- `GET/POST/PATCH/DELETE /admin/subjects[/{id}]`, nested `/chapters`, nested `/topics`
- `GET/POST/PATCH /admin/datasets[/{id}]` (no delete)

**School Admin — Roster**
- Teachers: `GET/POST /school/teachers`, `POST .../invite`, `POST .../bulk-invite` (multipart CSV/XLSX), `PATCH .../{id}/status`, `PATCH .../{id}` (edit), `POST .../{id}/reset-password`, `GET .../export` (CSV)
- Students: same shape — `/school/students`, `/bulk-create`, `/{id}`, `/{id}/reset-password`, `/export`
- Classes: `GET/POST /school/classes`, `PUT /school/classes/{id}/assignments`

**School Admin — Curriculum & Settings**
- `GET/PUT /school/curriculum` (enable/disable global subjects), `GET/PUT /school/datasets` (same for datasets)
- `GET/PUT /school/curriculum/default-bloom-distribution`
- `GET /school/subscription` — plan details + usage-vs-limit (teachers/students/tests/papers, null = unlimited)
- `GET/PATCH /school/me` — self-service profile edit
- `GET /school/analytics` — real computed metrics, school-scoped

**School Admin — School-wide Oversight** (`school_oversight.py`, read-only, paginated `{items, total}`)
- `GET /school/voice-tests` (`classId`, `teacherId`), `/school/homework` (`classId`), `/school/question-papers` (`subjectId`, `createdBy`), `/school/leaderboard`, `/school/audit-log`

**Teacher-facing** (`require_role(TEACHER)`)
- Curriculum browse: `/subjects`, `/classes/{id}/subjects`, `/subjects/{id}/chapters`, `/chapters/{id}/topics`; dataset browse
- Roster: `/classes`, `/classes/{id}`, `/classes/{id}/students`
- Voice Tests: list/get/create/schedule
- Test Results: class / students / one student
- Homework: create → generate → questions → edit/replace question → assign (full lifecycle)
- Question Papers: DRAFT → generate → questions → finalize (same lifecycle shape), candidate-shuffle picker, section reordering
- Teacher Dashboard, Assistant chat

**Student-facing** (`require_role(STUDENT)`)
- `/me/dashboard`, `/me/assigned-tests[/{id}]`, `/me/tests/{id}/bloom-result`
- `/me/homework/current` (+ context/questions/confirm-completion — confirming archives its questions into `/me/practice-bank`)
- `/me/journey`, `/me/practice-bank`, `/me/progress`, `/me/leaderboard`

**AI Assessor**
- `POST /ai-assessor/sessions` (opens a session)
- `POST /ai-assessor/sessions/{id}/realtime` — WebRTC SDP offer in, answer + `callId` out; audio then flows app ↔ OpenAI Realtime, colearner records the transcript server-side
- `POST /ai-assessor/sessions/{id}/finish` — ends the call, scores the server-side transcript, records the result
- `WS /ai-assessor/sessions/{id}/stream` — older per-turn voice stream bridged to colearner's chained/multimodal pipeline (`school.voice_pipeline`); nool-apps no longer uses it

**Health**: `GET /health`, `GET /ready` (both services)

---

## 5. Subscription & Plan-Limit Enforcement

Single source of truth for "what plan does this school have": `subscription_repository.get_active_plan()`, resolved via the `Subscription` row — never `School.plan_id`.

Single source of truth for "how much has this school used": `usage_repository`'s four counters (`count_teachers`, `count_students`, `count_tests`, `count_question_papers`) — shared between the enforcement checks (teacher invite, student create, voice test create, question paper create — each `409`s when a non-null limit is met) and `GET /school/subscription`'s proactive usage display, so there's exactly one counting implementation.

Limits are enforced as **lifetime totals** per school (not per billing period — there's no billing-cycle infrastructure yet). Null `test_limit`/`question_paper_limit` always means unlimited, represented as `null` in every response, never a fabricated large number or a `0`.

---

## 6. `nool-apps` (Mobile — Teacher & Student)

Route groups: `(auth)` (login, unknown-profile), `(app)/teacher`, `(app)/student`, plus `(app)/teacher/(question-paper)` and `(app)/teacher/(voice-test)` for multi-step flows. `app/index.tsx` is the root redirect gate; `app/change-password.tsx` sits outside `(auth)` deliberately, to avoid a redirect loop with that group's own "already authenticated → bounce home" logic.

### Teacher

| Feature | Backend | Status |
|---|---|---|
| Dashboard | `GET /teacher/dashboard` | Real |
| Classes/roster | `GET /classes` | Real |
| Curriculum/dataset browsing | `GET /subjects`, `GET /datasets` | Real |
| Voice Test setup/class/live/review | `GET/POST /tests` | Real |
| Test results (class & per-student) | `GET /tests/{id}/results/...` | Real |
| Question Paper authoring (incl. Bloom/difficulty mix editor) | `GET/POST /question-papers` | Real — always sends an explicit distribution, so the new school-level default is never actually exercised from this screen |
| Homework assign/live/review | `GET/POST /homework` | Real |
| Assistant chat | `GET/POST /assistant/messages` | Real transport; replies are keyword-matched canned text, not an LLM |
| **Settings** (Notifications/Appearance/Language/About) | none | **Placeholder** — every row routes to a shared "coming soon" stub |

### Student

| Feature | Backend | Status |
|---|---|---|
| Dashboard | `GET /me/dashboard` | Real |
| Assigned Tests | `GET /me/assigned-tests` | Real |
| AI Assessor voice session | `POST /ai-assessor/sessions` → `.../realtime` (WebRTC SDP offer/answer) → `.../finish` | Real — live full-duplex voice (OpenAI Realtime via colearner) |
| Homework | `GET /me/homework/current` | Real |
| Journey (chapter map) | `GET /me/journey` | Real |
| Practice Bank | `GET /me/practice-bank` | Real |
| Progress | `GET /me/progress` | Real |
| Leaderboard | `GET /me/leaderboard` | Real |

---

## 7. `nool-school-admin` (port 3100)

Strapi-inspired design system: indigo accent, dense sortable tables, light sidebar with pill-highlighted active nav. Shared components: `Toast`, `ConfirmDialog`, `CopyButton`, `CredentialReveal` (one-time temp-password display), `BulkUploadModal`, `AssignClassModal`.

| Page | Capabilities |
|---|---|
| `/teachers` | List, search, invite (temp-password reveal), edit, reset password, activate/deactivate, bulk-upload (CSV/XLSX + template), CSV export |
| `/teachers/[id]` | Drill-down: profile + their voice tests + question papers |
| `/students` | Same shape as Teachers, plus class filter |
| `/students/[id]` | Drill-down: profile + class-level homework (no true per-student filter exists in the API yet) |
| `/classes` | List, create, **Assign** modal (teacher + subject per class) |
| `/curriculum` | Enable/disable global Subjects for this school |
| `/datasets` | Enable/disable global Datasets for this school |
| `/subscription` | Plan details (name, renews date) + usage-vs-limit bars, "Unlimited" when null |
| `/analytics` | Real school-scoped metrics |
| `/question-defaults` | School's default Bloom/difficulty distribution (must sum to 100) |
| `/activity/*` (tests, homework, question-papers, leaderboard, audit-log) | Read-only paginated oversight tables |
| `/settings` | Self-service profile edit + proactive password change |
| `/change-password` (forced) | First-login password change |

---

## 8. `nool-super-admin` (port 3101)

Same design system and shared components as School Admin.

| Page | Capabilities |
|---|---|
| `/schools` | List, onboard (full profile metadata + plan) |
| `/schools/[id]` | Status toggle, subscription create/change/edit — **view-only** details card (backend edit endpoint exists, no edit UI yet) |
| `/plans` | Full CRUD, all limit fields, active toggle |
| `/subscriptions` | List view across schools |
| `/school-admins` | List, invite (temp-password reveal), activate/deactivate |
| `/curriculum` | Global Subjects→Chapters→Topics tree, full CRUD |
| `/datasets` | Global create/edit (no delete) |
| `/analytics` | Platform-wide real metrics |
| `/audit-log` | List view (not yet paginated, unlike School Admin's newer one) |

---

## 9. Known Gaps

Honestly tracked, not glossed over:

- **Super Admin has no self-service profile/password-change page** (School Admin has one; Super Admin doesn't yet).
- **School profile editing has a working backend endpoint (`PATCH /admin/schools/{id}`) but no Super Admin UI** for it yet.
- **Student drill-down activity is class-level, not per-student** — no student-level filter exists anywhere in the oversight API yet; the UI says so rather than faking precision.
- **Super Admin's audit log is unpaginated**, unlike the newer paginated pattern used everywhere else.
- **Teacher-app Settings screen is an unimplemented placeholder** (Notifications/Appearance/Language/About) — there is currently no persistent, saved teacher-level preference anywhere to build an admin override on top of, beyond the one that now exists (default Bloom/difficulty distribution).
- **Colearner state is in process memory** (sessions and live realtime calls) — lost on restart and not shared, so colearner runs as a single replica; Core's curriculum-sync WebSocket fan-out has the same single-instance limit.
- **Voice tests and question papers need the model APIs**: without `OPENAI_API_KEY`, or with kg / nool-kg unreachable, they fail (only Homework has a deterministic last-resort fallback).
- **Notifications are out of scope by design**, platform-wide.

---

## 10. Local Development

| Service | URL |
|---|---|
| NGINX gateway | http://localhost:8080 |
| Auth service (direct) | http://localhost:8001 |
| Core service (direct) | http://localhost:8002 |
| School Admin console | http://localhost:3100 |
| Super Admin console | http://localhost:3101 |
| Jaeger tracing | http://localhost:16686 |
| KG service (direct) | http://localhost:8003 |
| Colearner service (direct; playground at `/api/v1/colearner/playground`) | http://localhost:8004 |
| Generation service (direct) | http://localhost:8005 |
| PostgreSQL | localhost:5433 |

`docker compose up --build` from the repo root brings up nginx/auth/core/kg/generation/colearner/postgres/jaeger (+ azurite locally). See `docs/DEVELOPMENT.md` for the local nool-kg fallback and running tests. The two admin apps are separate repos/Compose projects (`nool-school-admin`, `nool-super-admin`), each with their own `compose.yml`.

**Known test account**: `schooladmin@test.com` / `Test1234!` (School Admin, confirmed working this session).
