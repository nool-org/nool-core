"""Import Nool's own Class 10 question bank for a subject - written from the NCERT
textbook pages and checked (independent solve, judge, evidence quote,
equation balance, originality vs the old scraped bank) - as the QA dataset
"Nool Original - <Subject>", and switch it on for every school with that subject.

Input: one JSON file per chapter (chNN.json, NN = NCERT chapter number), each
a list of questions with topic_id, type, marks, bloom, difficulty, text,
options, correct_option, answer, marking_scheme, evidence_quote, pages, status.
Only status "accept" is imported. Idempotent: upserts on (dataset_id, external_uid).
A picture question's drawing ("svg") is stored inline in figures, which the app draws.

Usage (host): python scripts/import_nool_bank.py --bank <dir with ch01.json ...> [--subject Mathematics]
              [--tag pic]   (picture bank: its own uids, so it sits beside the text bank)
"""
import argparse
import json
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

from import_dataset import db_config

# the bank's type -> (question_type, format)
TYPES = {"MCQ": ("MCQ", "mcq"), "ASSERTION_REASON": ("MCQ", "assertion-reason"),
         "SHORT_ANSWER": ("SHORT_ANSWER", "short"), "LONG_ANSWER": ("LONG_ANSWER", "long"),
         "CASE_BASED": ("LONG_ANSWER", "case-based")}


def row(q: dict, chapter_no: int, prefix: str) -> dict:
    figure = q.get("figure") or {}
    question_type, fmt = TYPES[q["type"]]
    if fmt == "short" and float(q["marks"]) <= 1:
        fmt = "very-short"
    options = q.get("options") or None
    mcq = q.get("correct_option") is not None and options
    scheme = "\n".join(f"- {p['point']} ({p['marks']})" for p in q.get("marking_scheme") or [])
    return {
        "external_uid": f"{prefix}-ch{chapter_no:02d}-{q['slot']}",
        "format": fmt, "question_type": question_type, "bloom_level": q["bloom"], "text": q["text"],
        "options": options if mcq else None,
        # an MCQ's answer is its right option (how every reader finds correct_index); the why is the solution
        "answer": options[q["correct_option"]] if mcq else q["answer"],
        "solution": "\n\n".join(x for x in (q["answer"] if mcq else None, scheme) if x) or None,
        "marks": float(q["marks"]), "difficulty": q["difficulty"],
        "source": {"sources": [{"dataset": "nool-original", "pages": q.get("pages"), "writer": q.get("writer")}],
                   "evidence": q.get("evidence_quote"), "answer_confidence": q.get("answer_confidence")},
        "figures": [{"kind": "diagram", "role": "question", "svg": q["svg"], "caption": figure.get("caption")}]
        if q.get("svg") else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", required=True, type=Path)
    ap.add_argument("--env", default=Path(".env"), type=Path)
    ap.add_argument("--subject", default="Science")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    name = f"Nool Original - {args.subject}"
    prefix = "nool-sci" if args.subject == "Science" else f"nool-{args.subject.lower()[:5]}"
    if args.tag:
        prefix += f"-{args.tag}"
    conn = psycopg2.connect(**db_config(args.env))
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = 10", (args.subject,))
        subject_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM users WHERE role = 'SUPER_ADMIN' ORDER BY created_at LIMIT 1")
        created_by = cur.fetchone()[0]
        cur.execute("SELECT id FROM datasets WHERE name = %s AND subject_id = %s", (name, subject_id))
        found = cur.fetchone()
        if not found:
            cur.execute("INSERT INTO datasets (id, name, question_count, description, restricted, type, subject_id, "
                        "board, grade) VALUES (%s, %s, 0, %s, false, 'QA', %s, 'CBSE', 10) RETURNING id",
                        (str(uuid.uuid4()), name, f"Nool's own CBSE Class 10 {args.subject} questions, written from "
                         "the NCERT textbook and checked", subject_id))
            found = cur.fetchone()
        dataset_id = found[0]

        cols = ["external_uid", "format", "question_type", "bloom_level", "text", "options", "answer", "solution",
                "marks", "difficulty", "source", "figures"]
        casts = {"question_type": "::question_type", "bloom_level": "::bloom_level",
                 "difficulty": "::paper_difficulty_level"}
        sql = (f"INSERT INTO dataset_questions (id, dataset_id, chapter_id, topic_id, created_by, {', '.join(cols)}) "
               f"VALUES (%s, %s, %s, %s, %s, {', '.join('%s' + casts.get(c, '') for c in cols)}) "
               "ON CONFLICT (dataset_id, external_uid) DO UPDATE SET chapter_id = EXCLUDED.chapter_id, "
               "topic_id = EXCLUDED.topic_id, " + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:]))
        total = 0
        for path in sorted(args.bank.glob("ch*.json")):
            chapter_no = int(path.stem[2:])
            for q in json.loads(path.read_text(encoding="utf-8")):
                if q["status"] != "accept":
                    continue
                cur.execute("SELECT chapter_id FROM topics WHERE id = %s", (q["topic_id"],))
                chapter_id = cur.fetchone()[0]
                r = row(q, chapter_no, prefix)
                cur.execute(sql, [str(uuid.uuid4()), dataset_id, chapter_id, q["topic_id"], created_by,
                                  *[Json(r[c]) if c in ("options", "source", "figures") and r[c] is not None else r[c]
                                    for c in cols]])
                total += 1
        cur.execute("UPDATE datasets SET question_count = (SELECT count(*) FROM dataset_questions WHERE dataset_id = %s)"
                    " WHERE id = %s", (dataset_id, dataset_id))
        # on for every school that teaches Science
        cur.execute("INSERT INTO school_datasets (id, school_id, dataset_id, enabled) "
                    "SELECT gen_random_uuid(), sc.school_id, %s, true FROM school_curriculum sc "
                    "WHERE sc.subject_id = %s AND sc.enabled "
                    "ON CONFLICT (school_id, dataset_id) DO UPDATE SET enabled = true", (dataset_id, subject_id))
        print(f"{name}: {total} questions imported, on for {cur.rowcount} school(s)")
    conn.close()


if __name__ == "__main__":
    main()
