"""Release smoke test against the RUNNING dev stack, through nginx only.

Drives the real HTTP APIs as a teacher and a student; the database is only
read (pick a chapter, watch prep_status). See scripts/e2e_smoke.md for how
to run it, how tokens are obtained, and how to clean up what it creates.

Exit code 0 = every step passed, 1 = any step failed. A failed setup step
skips the rest; a failed check (question quality, read paths) does not.
"""

import asyncio
import os
import sys
import time

import httpx

BASE = os.environ.get("BASE_URL", "http://nginx:80")
LEVELS = ["REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE", "EVALUATE"]  # the test's 5 fixed questions
PREP_TIMEOUT = int(os.environ.get("PREP_TIMEOUT", "240"))
LABEL = "SMOKE e2e"  # session context_label - the one free-text field a voice test flow stores

client = httpx.Client(base_url=BASE, timeout=30)
ctx: dict = {}
results: list[tuple[str, str, float, str]] = []


def token(role: str) -> str:
    """<ROLE>_TOKEN if given; else mint one for <ROLE>_EMAIL with the
    Firebase Admin SDK (custom token -> signInWithCustomToken). No password
    involved. Never printed."""
    if os.environ.get(f"{role}_TOKEN"):
        return os.environ[f"{role}_TOKEN"]
    email = os.environ.get(f"{role}_EMAIL")
    if not email:
        raise RuntimeError(f"set {role}_TOKEN or {role}_EMAIL")
    import firebase_admin
    from firebase_admin import auth, credentials

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(os.environ["FIREBASE_CREDENTIALS_PATH"]))
    custom = auth.create_custom_token(auth.get_user_by_email(email).uid).decode()
    r = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken",
        params={"key": os.environ["FIREBASE_WEB_API_KEY"]},
        json={"token": custom, "returnSecureToken": True}, timeout=30,
    )
    r.raise_for_status()
    return r.json()["idToken"]


def db(sql: str, *args):
    """Read-only query against the dev Postgres (compose .env credentials)."""
    import asyncpg

    async def run():
        conn = await asyncpg.connect(
            host=os.environ.get("POSTGRES_HOST", "postgres"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DB"])
        try:
            async with conn.transaction(readonly=True):
                return await conn.fetch(sql, *args)
        finally:
            await conn.close()
    return asyncio.run(run())


def call(method: str, path: str, who: str, expect: int = 200, **kw):
    r = client.request(method, path, headers={"Authorization": f"Bearer {ctx[who]}"}, **kw)
    if r.status_code != expect:
        raise AssertionError(f"{method} {path} -> {r.status_code} (expected {expect}): {r.text[:300]}")
    return r.json() if r.content else None


# --- steps: each returns a short detail string or raises -------------------

def s_health():
    r = client.get("/health")
    assert r.status_code == 200, f"/health -> {r.status_code}"
    return "nginx up"


def s_tokens():
    ctx["teacher"], ctx["student"] = token("TEACHER"), token("STUDENT")
    teacher, me = (call("GET", "/api/v1/me", who)["profile"] or {} for who in ("teacher", "student"))
    assert teacher.get("role") == "TEACHER", f"teacher token is a {teacher.get('role')}"
    assert me.get("role") == "STUDENT", f"student token is a {me.get('role')}"
    ctx["teacher_id"] = teacher["id"]
    ctx["student_id"] = me["id"]
    return f"teacher {ctx['teacher_id']}, student {ctx['student_id']}"


def s_pick_class():
    for c in call("GET", "/api/v1/classes", "teacher")["items"]:
        if c["grade"] != 10:
            continue
        students = call("GET", f"/api/v1/classes/{c['id']}/students", "teacher")["items"]
        if any(s["id"] == ctx["student_id"] for s in students):
            subjects = call("GET", f"/api/v1/classes/{c['id']}/subjects", "teacher")["items"]
            assert subjects, "teacher has no enabled subject in the class"
            ctx["class_id"], ctx["subject_id"] = c["id"], subjects[0]["id"]
            return f"Class 10{c['section']}, subject {subjects[0]['name']}"
    raise AssertionError("no Class 10 of the teacher has this student")


def s_pick_chapter():
    if os.environ.get("CHAPTER_ID"):
        ctx["chapter_id"], ctx["topic_id"] = os.environ["CHAPTER_ID"], os.environ["TOPIC_ID"]
        return "from env"
    # the topic with the most speakable dataset questions across all 5 levels
    rows = db("""
        select q.chapter_id::text, q.topic_id::text, count(*) n from dataset_questions q
        join datasets d on d.id = q.dataset_id join chapters c on c.id = q.chapter_id
        where d.type = 'QA' and d.grade = 10 and c.subject_id = $1::uuid and q.topic_id is not null
          and q.question_type not in ('MCQ', 'TRUE_FALSE') and q.bloom_level::text = any($2::text[])
        group by 1, 2 having count(distinct q.bloom_level) = 5 order by 3 desc limit 1""",
        ctx["subject_id"], LEVELS)
    assert rows, "no Class 10 chapter of this subject has dataset questions for all 5 levels"
    ctx["chapter_id"], ctx["topic_id"] = rows[0][0], rows[0][1]
    return f"chapter {rows[0][0]} ({rows[0][2]} candidate questions)"


def s_create():
    t = call("POST", "/api/v1/tests", "teacher", expect=201, json={
        "classId": ctx["class_id"], "subjectId": ctx["subject_id"], "chapterId": ctx["chapter_id"],
        "topicId": ctx["topic_id"], "bloomLevels": LEVELS, "durationMinutes": 10, "completionWindowHours": 24,
        "target": {"mode": "SPECIFIC_STUDENTS", "studentIds": [ctx["student_id"]]},
    })
    assert t["status"] == "DRAFT" and t["assignedCount"] == 1, t
    ctx["test_id"] = t["id"]
    return f"test {t['id']}"


def s_schedule():
    t = call("POST", f"/api/v1/tests/{ctx['test_id']}/schedule", "teacher")
    assert t["status"] == "SCHEDULED", t["status"]
    return "SCHEDULED"


def s_prep():
    deadline = time.monotonic() + PREP_TIMEOUT
    while True:
        status = db("select prep_status::text from voice_tests where id = $1::uuid", ctx["test_id"])[0][0]
        if status != "PENDING":
            break
        assert time.monotonic() < deadline, f"prep still PENDING after {PREP_TIMEOUT}s"
        time.sleep(3)
    assert status == "READY", f"prep_status {status}"
    return status


def s_questions():
    items = call("GET", f"/api/v1/tests/{ctx['test_id']}/questions", "teacher")["items"]
    levels = sorted(q["bloomLevel"] for q in items)
    assert levels == sorted(LEVELS), f"levels {levels}"
    issues = []
    for q in items:
        n = f"Q{q['order']} {q['bloomLevel']}"
        # no datasetQuestionId = written by generation because the bank had no speakable
        # question at that level (voice_test_prep._write_for_levels) - allowed, but noted
        if not (q["rewordedText"].strip() and q["spokenText"].strip()):
            issues.append(f"{n} empty text")
        elif q["datasetQuestionId"] and q["rewordedText"].strip() == q["originalText"].strip():
            issues.append(f"{n} not reworded")
        if not q["expectedPoints"]:
            issues.append(f"{n} no rubric points")
    assert not issues, "; ".join(issues)
    written = sum(1 for q in items if not q["datasetQuestionId"])
    return f"{len(items) - written} from the bank, {written} written; " + "; ".join(
        f"{q['bloomLevel']}: {q['spokenText'][:60]}" for q in items)


def s_assigned():
    ids = [t["id"] for t in call("GET", "/api/v1/me/assigned-tests", "student")["items"]]
    assert ctx["test_id"] in ids, "test not in the student's assigned tests"
    return f"{len(ids)} assigned"


def s_open_session():
    s = call("POST", "/api/v1/ai-assessor/sessions", "student", expect=201, json={
        "testId": ctx["test_id"], "contextLabel": LABEL, "bloomLevels": LEVELS, "durationSeconds": 600})
    assert s["questionCount"] == len(LEVELS), f"questionCount {s['questionCount']}"
    return f"session {s['sessionId']}, questionCount {s['questionCount']}"


def s_dashboard():
    call("GET", "/api/v1/me/dashboard", "student")
    return "200"


def s_insights():
    call("GET", f"/api/v1/me/tests/{ctx['test_id']}/insights", "student", expect=404)
    return "404 before a result"


def s_mcq():
    return f"{len(call('GET', '/api/v1/me/mcq-tests', 'student')['items'])} MCQ tests"


def s_reviews():
    return f"{len(call('GET', f'/api/v1/tests/{ctx['test_id']}/reviews', 'teacher')['items'])} awaiting review"


STEPS = [
    ("nginx health", s_health, True),
    ("tokens + /me", s_tokens, True),
    ("teacher's Class 10 with the student", s_pick_class, True),
    ("chapter with dataset questions", s_pick_chapter, True),
    ("teacher creates voice test", s_create, True),
    ("teacher schedules it", s_schedule, True),
    ("question prep READY", s_prep, True),
    ("1 reworded question per Bloom level", s_questions, False),
    ("student sees it assigned", s_assigned, False),
    ("student opens session", s_open_session, False),
    # Finishing needs a live realtime call: core's /finish asks colearner for
    # the call's C3 payload and exposes no test hook, so the voice part stops here.
    ("student dashboard", s_dashboard, False),
    ("insights 404 before result", s_insights, False),
    ("MCQ test list", s_mcq, False),
    ("teacher review list", s_reviews, False),
]


def main() -> int:
    failed = blocked = False
    for name, fn, fatal in STEPS:
        if blocked:
            results.append(("SKIP", name, 0.0, ""))
            continue
        start = time.monotonic()
        try:
            status, detail = "PASS", fn()
        except Exception as exc:  # report every failure the same way
            status, detail, failed = "FAIL", f"{type(exc).__name__}: {exc}", True
            blocked = fatal
        results.append((status, name, time.monotonic() - start, detail))
        print(f"{status}  {name:<38} {results[-1][2]:6.1f}s  {detail}", flush=True)
    passed = sum(r[0] == "PASS" for r in results)
    print(f"\n{'OK' if not failed else 'FAILED'}: {passed}/{len(STEPS)} passed, "
          f"{sum(r[0] == 'SKIP' for r in results)} skipped, {sum(r[2] for r in results):.1f}s total")
    if "test_id" in ctx:
        print(f"created: voice test {ctx['test_id']} (see e2e_smoke.md for cleanup)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
