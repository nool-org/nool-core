"""Full end-to-end validation against the RUNNING dev stack, through nginx.

1. GET sweep: every GET route in core's OpenAPI, called as the role that owns
   it, with path ids harvested from list responses. Flags 5xx and 4xx.
2. Write flows: question paper (generate, copilot, answer key, Set B,
   finalize), templates (starter use, design/edit/duplicate/delete), homework
   (create, generate, assign, student answers), MCQ attempt.

Makes live model calls (paper generation, chat, answer key, homework): about
$0.30 a run. Creates data tagged "E2E" (see the summary at the end).
Run like e2e_smoke.py (see e2e_smoke.md) plus -e ADMIN_EMAIL=admin@nool.dev
-e SCHOOL_ADMIN_EMAIL=keerthana@nool.dev, with: python /s/e2e_full.py
"""

import re
import sys
import time

import httpx

sys.path.insert(0, "/s")
from e2e_smoke import token  # noqa: E402

API = "http://nginx:80/api/v1"
ROLES = {"teacher": "TEACHER", "student": "STUDENT", "school": "SCHOOL_ADMIN", "admin": "ADMIN"}
tok = {r: token(env) for r, env in ROLES.items()}
c = httpx.Client(timeout=180)
problems: list[str] = []
created: list[str] = []
count = {"ok": 0, "4xx": 0, "5xx": 0}


def req(who, method, path, expect=(200, 201), quiet=False, **kw):
    t = time.monotonic()
    r = c.request(method, API + path, headers={"Authorization": f"Bearer {tok[who]}"}, **kw)
    ms = (time.monotonic() - t) * 1000
    ok = r.status_code in expect
    if not quiet or not ok:
        print(f"{'OK ' if ok else 'BAD'} {r.status_code} {ms:6.0f}ms {who:7} {method} {path}", flush=True)
    if not ok:
        problems.append(f"{who} {method} {path} -> {r.status_code}: {r.text[:200]}")
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def items(body):
    return (body or {}).get("items", []) if isinstance(body, dict) else (body or [])


# ---- 1. GET sweep ------------------------------------------------------------

def owner(path):
    if path.startswith("/admin"):
        return "admin"
    if path.startswith("/school"):
        return "school"
    if path.startswith("/me"):
        return "student"
    return "teacher"


ids: dict[str, list[str]] = {}
HARVEST = {  # list path -> param name its items' ids fill
    "/admin/schools": "school_id", "/admin/plans": "plan_id", "/admin/datasets": "dataset_id",
    "/admin/tickets": "ticket_id", "/admin/reports/configs": "config_id", "/admin/subjects": "subject_id",
    "/school/tickets": "ticket_id", "/school/grades": "grade_id", "/school/reports/shared": "share_id",
    "/classes": "class_id", "/tests": "test_id", "/homework": "homework_id",
    "/question-papers": "paper_id", "/question-paper-templates": "template_id",
    "/subjects": "subject_id", "/me/mcq-tests": "mcq_test_id",
}


def sweep():
    spec = httpx.get("http://core:8000/openapi.json").json()["paths"]
    gets = sorted(p.removeprefix("/api/v1") for p, ops in spec.items() if "get" in ops and p.startswith("/api/v1"))
    plain = [p for p in gets if "{" not in p and p not in ("/me/journey", "/admin/curriculum/kg-tree")]
    # lists first so their ids are there for the templated paths
    for p in sorted(plain, key=lambda p: p not in HARVEST):
        _, body = req(owner(p), "GET", p, quiet=True)
        if p in HARVEST:
            ids.setdefault(HARVEST[p], []).extend(str(i["id"]) for i in items(body)[:2] if "id" in i)
    # derived pools
    for sid in ids.get("subject_id", [])[:1]:
        _, b = req("teacher", "GET", f"/subjects/{sid}/chapters", quiet=True)
        ids["chapter_id"] = [i["id"] for i in items(b)[:1]]
    for ch in ids.get("chapter_id", []):
        _, b = req("teacher", "GET", f"/chapters/{ch}/topics", quiet=True)
        ids["topic_id"] = [i["id"] for i in items(b)[:1]]
    for cid in ids.get("class_id", [])[:1]:
        _, b = req("teacher", "GET", f"/classes/{cid}/students", quiet=True)
        ids["student_id"] = [i["id"] for i in items(b)[:1]]
    _, a = req("student", "GET", "/me/assigned-tests", quiet=True)
    for t in items(a)[:1]:
        req("student", "GET", f"/me/journey?subjectId={t['subjectId']}", quiet=True)
    templated = [p for p in gets if "{" in p and not p.endswith("/kg-tree")]
    for sid in ids.get("subject_id", [])[:1]:
        req("admin", "GET", f"/admin/subjects/{sid}/kg-tree?grade=10", quiet=True)
    skipped = []
    for p in templated:
        names = re.findall(r"\{(\w+)\}", p)
        if not all(ids.get(n) for n in names):
            skipped.append(p)
            continue
        path = p
        for n in names:
            path = path.replace("{" + n + "}", ids[n][0])
        who = owner(p)
        if p == "/me/mcq-tests/{mcq_test_id}":
            req(who, "GET", path, expect=(200, 409), quiet=True)  # 409 = locked until the voice test result
        elif p.startswith("/me/tests/") or p.startswith("/me/assigned-tests/"):
            # 404 is the documented answer before the student has a result / once it's done
            req(who, "GET", path, expect=(200, 404), quiet=True)
        elif p.endswith("/export"):
            req(who, "GET", path, quiet=True)
        else:
            req(who, "GET", path, quiet=True)
    print(f"sweep: {len(plain)} plain + {len(templated) - len(skipped)} templated GETs; "
          f"no ids for {len(skipped)}: {', '.join(skipped)}")


# ---- 2. flows ----------------------------------------------------------------

def step(name):
    print(f"\n== {name}", flush=True)


def paper_flow():
    step("question paper: create -> generate -> copilot -> answer key -> Set B -> finalize")
    _, classes = req("teacher", "GET", "/classes")
    cl = next(c_ for c_ in items(classes) if c_["grade"] == 10)
    _, subs = req("teacher", "GET", f"/classes/{cl['id']}/subjects")
    sub = items(subs)[0]
    _, chs = req("teacher", "GET", f"/subjects/{sub['id']}/chapters?grade=10")
    chapter_ids = [ch["id"] for ch in items(chs)[:2]]
    st, paper = req("teacher", "POST", "/question-papers", expect=(201,), json={
        "name": "E2E paper", "examType": "Unit Test", "board": "CBSE", "grade": 10, "language": "English",
        "subjectId": sub["id"], "chapterIds": chapter_ids, "totalMarks": 20, "durationMinutes": 45,
        "questionTypes": ["MCQ", "SHORT_ANSWER"],
        "sections": [{"label": "A", "questionCount": 5, "marksEach": 1},
                     {"label": "B", "questionCount": 3, "marksEach": 5}],
    })
    if st != 201:
        return
    pid = paper["id"]
    created.append(f"paper {pid}")
    t = time.monotonic()
    st, gen = req("teacher", "POST", f"/question-papers/{pid}/generate")
    print(f"   generate took {time.monotonic() - t:.0f}s, status {(gen or {}).get('status')}")
    _, qs = req("teacher", "GET", f"/question-papers/{pid}/questions")
    qs = items(qs)
    src = {}
    for q in qs:
        src[q.get("source")] = src.get(q.get("source"), 0) + 1
    print(f"   {len(qs)} questions, sources {src}")
    if len(qs) != 8:
        problems.append(f"paper: asked for 8 questions, got {len(qs)}")
    if not src.get("bank"):
        problems.append("paper: no question came from the dataset bank (all AI-written)")
    _, rc = req("teacher", "GET", f"/question-papers/{pid}/rules-check")
    print(f"   rules-check ok={rc and rc.get('ok')} issues={[i['code'] for i in (rc or {}).get('issues', [])]}")
    if not qs:
        return
    req("teacher", "GET", f"/question-papers/{pid}/questions/{qs[0]['id']}/candidates")
    req("teacher", "POST", f"/question-papers/{pid}/questions/{qs[0]['id']}/explain")
    _, chat = req("teacher", "POST", f"/question-papers/{pid}/chat",
                  json={"message": "Make question 1 a little easier and simpler to read."})
    print(f"   chat reply: {str((chat or {}).get('reply') or chat)[:160]}")
    proposals = (chat or {}).get("proposals") or []
    if proposals:
        req("teacher", "POST", f"/question-papers/{pid}/actions",
            json={"ops": [p["op"] for p in proposals], "summary": proposals[0]["reason"][:200], "origin": "chat"})
    else:
        problems.append(f"copilot chat returned no proposal ops: keys {list((chat or {}).keys())}")
    req("teacher", "POST", f"/question-papers/{pid}/actions", json={
        "ops": [{"op": "edit", "questionId": qs[-1]["id"], "changes": {"marks": qs[-1]["marks"]}}],
        "summary": "manual no-op edit", "origin": "manual"})
    _, vers = req("teacher", "GET", f"/question-papers/{pid}/versions")
    vers = items(vers)
    print(f"   versions: {[v['number'] for v in vers]}")
    if len(vers) >= 2:
        req("teacher", "POST", f"/question-papers/{pid}/versions/{vers[-1]['id']}/restore")
    req("teacher", "GET", f"/question-papers/{pid}/chat")
    t = time.monotonic()
    st, ak = req("teacher", "POST", f"/question-papers/{pid}/answer-key/generate")
    print(f"   answer key took {time.monotonic() - t:.0f}s")
    _, ak = req("teacher", "GET", f"/question-papers/{pid}/answer-key")
    missing = [i for i in items(ak) if not (i.get("answer") or i.get("modelAnswer"))]
    if items(ak) and missing:
        problems.append(f"answer key: {len(missing)} questions without an answer")
    st, setb = req("teacher", "POST", f"/question-papers/{pid}/parallel", expect=(201,), json={"name": "E2E paper Set B"})
    if st == 201:
        created.append(f"paper {setb.get('paperId')} (Set B)")
        _, bq = req("teacher", "GET", f"/question-papers/{setb['paperId']}/questions")
        print(f"   Set B: {len(items(bq))} questions, status {setb.get('status')}")
    st, tpl = req("teacher", "POST", "/question-paper-templates", expect=(201,), json={"fromPaperId": pid, "name": "E2E from paper"})
    if st == 201:
        req("teacher", "DELETE", f"/question-paper-templates/{tpl['id']}", expect=(200, 204))
    req("teacher", "POST", f"/question-papers/{pid}/finalize")
    req("teacher", "POST", f"/question-papers/{pid}/chat", expect=(409, 422, 400), json={"message": "change q1"})
    return sub


def template_flow(sub):
    step("templates: starters, use starter -> generate, design / edit / duplicate / delete, validation")
    _, lst = req("teacher", "GET", "/question-paper-templates?scope=starter")
    starters = items(lst)
    print(f"   {len(starters)} starters: {[s['name'] for s in starters]}")
    unit = next((s for s in starters if s["grade"] == 10 and s["totalMarks"] == 25), None)
    if unit:
        req("teacher", "POST", f"/question-paper-templates/{unit['id']}/use", expect=(422, 400), json={"name": "E2E no subject"})
        _, chs = req("teacher", "GET", f"/subjects/{sub['id']}/chapters?grade=10")
        st, used = req("teacher", "POST", f"/question-paper-templates/{unit['id']}/use",
                       expect=(200, 201), json={"name": "E2E from starter", "subjectId": sub["id"],
                                                "chapterIds": [ch["id"] for ch in items(chs)[:1]]})
        if used:
            created.append(f"paper {used['paperId']} (starter)")
            t = time.monotonic()
            _, gen = req("teacher", "POST", f"/question-papers/{used['paperId']}/generate")
            _, rc = req("teacher", "GET", f"/question-papers/{used['paperId']}/rules-check")
            print(f"   starter paper generated in {time.monotonic() - t:.0f}s: status {(gen or {}).get('status')}, "
                  f"marks {(rc or {}).get('totalMarks')}, issues {[i['code'] for i in (rc or {}).get('issues', [])]}")
        req("teacher", "POST", f"/question-paper-templates/{unit['id']}/duplicate", expect=(200, 201), json={})
        req("teacher", "PATCH", f"/question-paper-templates/{unit['id']}", expect=(403,), json={"name": "x"})
    else:
        problems.append("no Class 10 25-mark starter template")
    body = {"name": "E2E designed", "scope": "personal", "examType": "Unit Test", "board": "CBSE", "grade": 10,
            "subjectId": sub["id"], "language": "English", "totalMarks": 20, "durationMinutes": 40,
            "sections": [{"name": "A", "questionCount": 10, "marksEach": 1, "questionTypes": ["MCQ"]},
                         {"name": "B", "questionCount": 2, "marksEach": 5, "questionTypes": ["SHORT_ANSWER"]}],
            "bloomTargets": [{"level": "REMEMBER", "percent": 40}, {"level": "UNDERSTAND", "percent": 40},
                             {"level": "APPLY", "percent": 20}],
            "difficultyMix": {"easy": 40, "medium": 40, "hard": 20}}
    req("teacher", "POST", "/question-paper-templates", expect=(422,), json={**body, "totalMarks": 25})
    req("teacher", "POST", "/question-paper-templates", expect=(422,), json={**body, "difficultyMix": {"easy": 50, "medium": 40, "hard": 20}})
    req("teacher", "POST", "/question-paper-templates", expect=(403,), json={**body, "scope": "school"})
    st, t = req("teacher", "POST", "/question-paper-templates", expect=(201,), json=body)
    if st == 201:
        req("teacher", "PATCH", f"/question-paper-templates/{t['id']}", json={"durationMinutes": 50})
        req("teacher", "GET", f"/question-paper-templates/{t['id']}")
        req("student", "GET", f"/question-paper-templates/{t['id']}", expect=(403, 404))
        _, dup = req("teacher", "POST", f"/question-paper-templates/{t['id']}/duplicate", expect=(200, 201), json={"name": "E2E copy"})
        for tid in (t["id"], (dup or {}).get("id")):
            if tid:
                req("teacher", "DELETE", f"/question-paper-templates/{tid}", expect=(200, 204))
    st, s = req("school", "POST", "/question-paper-templates", expect=(201,), json={**body, "scope": "school", "name": "E2E school tpl"})
    if st == 201:
        _, mine = req("teacher", "GET", "/question-paper-templates?scope=school")
        if s["id"] not in [i["id"] for i in items(mine)]:
            problems.append("school-scope template not visible to the school's teacher")
        req("school", "DELETE", f"/question-paper-templates/{s['id']}", expect=(200, 204))
    _, mine = req("teacher", "GET", "/question-paper-templates?scope=personal")
    for i in items(mine):  # starter duplicate from above
        if i["name"].startswith(("Unit", "E2E")) and i.get("canDelete"):
            req("teacher", "DELETE", f"/question-paper-templates/{i['id']}", expect=(200, 204), quiet=True)


def homework_flow():
    step("homework: create -> generate -> assign -> student answers -> complete")
    _, tests = req("teacher", "GET", "/tests")
    _, dss = req("teacher", "GET", "/datasets")
    test = next((t for t in items(tests) if t.get("status") == "RESULTS_READY"), None)
    if test is None:
        print(f"   no RESULTS_READY voice test (statuses {sorted({t.get('status') for t in items(tests)})}) - homework needs one")
        return
    ds = [d["id"] for d in items(dss) if d.get("grade") in (10, None)][:1]
    st, hw = req("teacher", "POST", "/homework", expect=(201,), json={
        "testId": test["id"], "datasetIds": ds, "totalQuestions": 4, "difficulty": "MIXED",
        "bloomDistribution": [{"level": "REMEMBER", "value": 50}, {"level": "UNDERSTAND", "value": 50}]})
    if st != 201:
        return
    hid = hw["id"]
    created.append(f"homework {hid}")
    t = time.monotonic()
    _, g = req("teacher", "POST", f"/homework/{hid}/generate")
    _, hq = req("teacher", "GET", f"/homework/{hid}/questions")
    print(f"   generated {len(items(hq))} questions in {time.monotonic() - t:.0f}s, status {(g or {}).get('status')}")
    _, me = req("student", "GET", "/me")
    req("teacher", "POST", f"/homework/{hid}/assign", json={
        "target": {"mode": "SPECIFIC_STUDENTS", "studentIds": [me["profile"]["id"]]}, "completionWindowHours": 24})
    req("student", "GET", f"/me/homework/{hid}")
    req("student", "GET", f"/me/homework/{hid}/context")
    _, sq = req("student", "GET", f"/me/homework/{hid}/questions")
    xp = 0
    for q in items(sq):
        _, a = req("student", "POST", f"/me/homework/{hid}/questions/{q['id']}/answer",
                   json={"answerText": "Because of reflection of light.", "selfRating": "got_it"})
        xp += (a or {}).get("xpAwarded", 0)
    print(f"   answered {len(items(sq))}, xp {xp}")
    req("student", "POST", f"/me/homework/{hid}/confirm-completion")
    req("student", "GET", "/me/gamification")


def mcq_flow():
    step("MCQ: attempt an open MCQ test if one exists")
    _, lst = req("student", "GET", "/me/mcq-tests")
    print(f"   statuses: {[m.get('status') for m in items(lst)]}")
    open_ = [m for m in items(lst) if str(m.get("status")).lower() == "available"]
    if not open_:
        print(f"   no open MCQ test ({len(items(lst))} listed) - unlocks after a voice test result")
        return
    _, m = req("student", "GET", f"/me/mcq-tests/{open_[0]['id']}")
    answers = [{"questionId": q["id"], "optionIndex": 0} for q in (m or {}).get("questions", [])]
    req("student", "POST", f"/me/mcq-tests/{open_[0]['id']}/attempt", json={"answers": answers})


def main():
    step("GET sweep")
    sweep()
    sub = None
    for flow in (paper_flow, template_flow, homework_flow, mcq_flow):
        try:
            if flow is template_flow:
                if sub:
                    flow(sub)
            else:
                out = flow()
                sub = out if flow is paper_flow else sub
        except Exception as e:
            problems.append(f"{flow.__name__} crashed: {type(e).__name__}: {e}")
    print("\n== PROBLEMS" if problems else "\n== no problems")
    for p in problems:
        print(" -", p)
    print("created:", "; ".join(created))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
