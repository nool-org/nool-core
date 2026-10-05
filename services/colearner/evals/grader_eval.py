"""Grader accuracy gate: grades every labelled transcript with the production grader N times.

Run inside the colearner image (needs OPENAI_API_KEY):
    python evals/grader_eval.py [--repeats 2] [--data evals/grader_set.json]
Exit code 1 if verdict accuracy < 95% or more than 1 in 30 wrong answers are marked correct.
"""
import argparse
import asyncio
import json
import re
import statistics
import sys
from pathlib import Path

MIN_ACCURACY = 0.95
MAX_FALSE_PASS_RATE = 1 / 30
MIN_OVERLAP = 0.15  # word-set Jaccard needed to match a graded answer to a labelled one

ROLES = {"co_learner": "CO-LEARNER", "student": "STUDENT"}


def transcript(session: dict) -> str:
    return "\n".join(f"[{ROLES[t['role']]}]: {t['text']}" for t in session["turns"])


def _words(s: str) -> set:
    return set(re.sub(r"[^a-z0-9' ]", " ", (s or "").lower()).split())


def overlap(a: str, b: str) -> float:
    A, B = _words(a), _words(b)
    return len(A & B) / max(1, len(A | B))


def align(session: dict, report: dict) -> list:
    """One row per labelled student answer: {label, verdict (True/False/None if the grader skipped it), score}."""
    labelled = [t for t in session["turns"] if t.get("label")]
    rows = [{"label": t["label"], "text": t["text"], "verdict": None, "score": None} for t in labelled]
    used = set()
    for qa in report.get("qa_transcripts") or []:
        resp = qa.get("student_response") or ""
        free = [i for i in range(len(rows)) if i not in used]
        best = max(free, key=lambda i: overlap(resp, rows[i]["text"]), default=None)
        if best is None or overlap(resp, rows[best]["text"]) < MIN_OVERLAP:
            continue
        used.add(best)
        rows[best]["verdict"] = qa.get("is_substantively_correct")
        rows[best]["score"] = qa.get("score_percent")
    return rows


def mastery_gap(report: dict, rows: list) -> float | None:
    """|reported mastery - mean score of the answers we could match| for one graded session."""
    mastery = (report.get("session_summary") or {}).get("overall_mastery_percent")
    scores = [r["score"] for r in rows if isinstance(r["score"], (int, float))]
    if not isinstance(mastery, (int, float)) or not scores:
        return None
    return abs(mastery - statistics.mean(scores))


def summarize(rows: list, gaps: list, cost_usd: float, errors: int) -> dict:
    """Ambiguous answers are excluded. A skipped answer counts as a wrong verdict (but never as a false pass)."""
    scored = [r for r in rows if r["label"] in ("correct", "wrong")]
    right = [r for r in scored if r["label"] == "correct"]
    wrong = [r for r in scored if r["label"] == "wrong"]
    hits = sum(r["verdict"] is (r["label"] == "correct") for r in scored)
    false_pass = sum(r["verdict"] is True for r in wrong)
    false_fail = sum(r["verdict"] is False for r in right)
    accuracy = hits / len(scored) if scored else 0.0
    false_pass_rate = false_pass / len(wrong) if wrong else 0.0
    return {
        "answers_scored": len(scored),
        "verdict_accuracy_pct": round(100 * accuracy, 1),
        "wrong_marked_correct": f"{false_pass}/{len(wrong)}",
        "right_marked_wrong": f"{false_fail}/{len(right)}",
        "skipped_by_grader": sum(r["verdict"] is None for r in scored),
        "grading_errors": errors,
        "mastery_gap_mean": round(statistics.mean(gaps), 1) if gaps else None,
        "mastery_gap_max": round(max(gaps), 1) if gaps else None,
        "cost_usd": round(cost_usd, 4),
        "passed": accuracy >= MIN_ACCURACY and false_pass_rate <= MAX_FALSE_PASS_RATE,
    }


async def run(data: dict, repeats: int) -> dict:
    from src.domain.schemas import SessionConfig
    from src.services.evaluation import grade_transcript
    from src.services.session_store import SessionState

    sem = asyncio.Semaphore(6)

    async def grade(s: dict):
        n = sum(1 for t in s["turns"] if t.get("label"))
        state = SessionState(session_id=f"eval-{s['id']}", config=SessionConfig(
            grade=s["grade"], subject=s["subject"], chapter_name=s["chapter"], num_questions=min(10, max(1, n))))
        async with sem:
            report = await grade_transcript(state, transcript(s), rubric=s.get("rubric"))
        return s, report, state.total_cost_usd

    results = await asyncio.gather(*(grade(s) for _ in range(repeats) for s in data["sessions"]))
    rows, gaps, cost, errors, misses = [], [], 0.0, 0, []
    for s, report, c in results:
        cost += c
        errors += "grading_error" in report
        r = align(s, report)
        rows += r
        gap = mastery_gap(report, r)
        if gap is not None:
            gaps.append(gap)
        misses += [f"{s['id']}: {x['label']} answer judged {x['verdict']}: {x['text'][:90]}"
                   for x in r if x["label"] != "ambiguous" and x["verdict"] is not (x["label"] == "correct")]
    return {**summarize(rows, gaps, cost, errors), "repeats": repeats, "misjudged": misses}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--data", default=str(Path(__file__).with_name("grader_set.json")))
    args = ap.parse_args()
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    result = asyncio.run(run(data, args.repeats))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("GRADER GATE:", "PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
