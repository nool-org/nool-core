"""Grader accuracy gate: grades every labelled transcript with the production grader N times.

Run inside the colearner image (needs OPENAI_API_KEY):
    python evals/grader_eval.py [--repeats 2] [--data evals/grader_set.json]
Exit code 1 if verdict accuracy < 95%, more than 1 in 30 wrong answers are marked correct, a labelled
answer's score falls outside its min_score/max_score, or a student_response has a [bracketed] note.
score_spread_max (one answer's max - min score across repeats) is reported, not gated yet (AI-007).
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
    """The production format (realtime_service._transcript_text): [Qn] tags and (kind) for non-answers."""
    return "\n".join(
        (f"[Q{t['question_number']}] " if t.get("question_number") else "")
        + f"[{ROLES[t['role']]}]"
        + (f" ({t['kind'].replace('_', ' ')})" if t.get("kind") not in (None, "answer") else "")
        + f": {t['text']}"
        for t in session["turns"]
    )


def _words(s: str) -> set:
    return set(re.sub(r"[^a-z0-9' ]", " ", (s or "").lower()).split())


def overlap(a: str, b: str) -> float:
    A, B = _words(a), _words(b)
    return len(A & B) / max(1, len(A | B))


def align(session: dict, report: dict) -> list:
    """One row per labelled student answer: {label, verdict (True/False/None if the grader skipped it), score}."""
    labelled = [t for t in session["turns"] if t.get("label")]
    rows = [{"label": t["label"], "text": t["text"], "verdict": None, "score": None, "no_answer": t.get("no_answer", False),
             "min_score": t.get("min_score", 0), "max_score": t.get("max_score", 100)} for t in labelled]
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
    # a no_answer turn (e.g. the transcription prompt echoed back) may also be skipped by the grader
    hits = sum(r["verdict"] is (r["label"] == "correct") or (r["verdict"] is None and bool(r.get("no_answer"))) for r in scored)
    band_misses = [r for r in scored if isinstance(r.get("score"), (int, float))
                   and not r.get("min_score", 0) <= r["score"] <= r.get("max_score", 100)]
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
        "score_band_misses": len(band_misses),
        "grading_errors": errors,
        "mastery_gap_mean": round(statistics.mean(gaps), 1) if gaps else None,
        "mastery_gap_max": round(max(gaps), 1) if gaps else None,
        "cost_usd": round(cost_usd, 4),
        "passed": accuracy >= MIN_ACCURACY and false_pass_rate <= MAX_FALSE_PASS_RATE and not band_misses,
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
    rows, gaps, cost, errors, misses, by_answer, bracketed = [], [], 0.0, 0, [], {}, 0
    for s, report, c in results:
        cost += c
        errors += "grading_error" in report
        bracketed += sum(bool(re.search(r"[\[\]]", qa.get("student_response") or ""))
                         for qa in report.get("qa_transcripts") or [])
        r = align(s, report)
        rows += r
        for i, x in enumerate(r):
            if isinstance(x["score"], (int, float)):
                by_answer.setdefault((s["id"], i), []).append(x["score"])
        gap = mastery_gap(report, r)
        if gap is not None:
            gaps.append(gap)
        misses += [f"{s['id']}: {x['label']} answer judged {x['verdict']}: {x['text'][:90]}"
                   for x in r if x["label"] != "ambiguous" and x["verdict"] is not (x["label"] == "correct")
                   and not (x["verdict"] is None and x["no_answer"])]
        misses += [f"{s['id']}: score {x['score']} outside {x['min_score']}-{x['max_score']}: {x['text'][:90]}"
                   for x in r if x["label"] != "ambiguous" and isinstance(x["score"], (int, float))
                   and not x["min_score"] <= x["score"] <= x["max_score"]]
    summary = summarize(rows, gaps, cost, errors)
    spread = max((max(v) - min(v) for v in by_answer.values()), default=0)
    return {**summary, "score_spread_max": spread, "bracketed_student_responses": bracketed,
            "passed": summary["passed"] and not bracketed, "repeats": repeats, "misjudged": misses}


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
