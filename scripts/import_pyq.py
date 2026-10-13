"""Import CBSE board-paper questions (nool-kg scripts/ingest_pyq.py ->
output/pyq/Class10_Mathematics.json) into a "CBSE Board Papers" QA dataset
for Mathematics, Class 10.

- Chapters: matched on the NCERT chapter number (kg_ref CH01..CH14, the ids
  nool-kg uses), created when missing so a later textbook sync renames
  rather than duplicates them.
- Topics: only where the chapter's topics exist - Real Numbers by keyword
  (irrational -> 1.3, else 1.2).
- Questions needing a figure from the paper are skipped (text only).
- Every school with Mathematics enabled gets the dataset switched on.
Idempotent: upserts on (dataset_id, external_uid).

Usage (host): python scripts/import_pyq.py --data C:/pet/nool-kg/output/pyq/Class10_Mathematics.json
"""
import argparse
import json
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

from import_dataset import db_config

CHAPTERS = [
    "Real Numbers", "Polynomials", "Pair of Linear Equations in Two Variables", "Quadratic Equations",
    "Arithmetic Progressions", "Triangles", "Coordinate Geometry", "Introduction to Trigonometry",
    "Some Applications of Trigonometry", "Circles", "Areas Related to Circles", "Surface Areas and Volumes",
    "Statistics", "Probability",
]
TYPE = {"mcq": "MCQ", "assertion_reason": "MCQ", "short": "SHORT_ANSWER", "long": "LONG_ANSWER",
        "case_study": "LONG_ANSWER"}
DATASET = "CBSE Board Papers"


def topic_ref(q: dict) -> str | None:
    """Real Numbers topics by keyword; other chapters' topics aren't ingested yet."""
    if q["chapter"] != "Real Numbers":
        return None
    return "CH01_T03" if "irrational" in q["text"].lower() or "rational" in q["answer"].lower() else "CH01_T02"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--subject-name", default="Mathematics")
    ap.add_argument("--grade", type=int, default=10)
    args = ap.parse_args()
    questions = [q for q in json.loads(args.data.read_text(encoding="utf-8")) if not q.get("needs_figure")]

    conn = psycopg2.connect(**db_config(Path(__file__).resolve().parent.parent / ".env"))
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = %s", (args.subject_name, args.grade))
        subject_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM users WHERE role = 'SUPER_ADMIN' ORDER BY created_at LIMIT 1")
        created_by = cur.fetchone()[0]

        chapter_ids, topic_ids = {}, {}
        for n, name in enumerate(CHAPTERS, start=1):
            ref = f"CH{n:02d}"
            cur.execute("SELECT id FROM chapters WHERE subject_id = %s AND kg_ref = %s", (subject_id, ref))
            found = cur.fetchone()
            if not found:
                cur.execute("INSERT INTO chapters (id, subject_id, name, order_index, grade, kg_ref) "
                            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                            (str(uuid.uuid4()), subject_id, name, n, args.grade, ref))
                found = cur.fetchone()
            chapter_ids[name] = found[0]
        cur.execute("SELECT t.kg_ref, t.id FROM topics t WHERE t.chapter_id = %s", (chapter_ids["Real Numbers"],))
        topic_ids = dict(cur.fetchall())

        cur.execute("SELECT id FROM datasets WHERE name = %s AND subject_id = %s AND grade = %s AND board = 'CBSE'",
                    (DATASET, subject_id, args.grade))
        found = cur.fetchone()
        if not found:
            cur.execute("INSERT INTO datasets (id, name, question_count, description, restricted, type, subject_id, "
                        "board, grade) VALUES (%s, %s, 0, %s, false, 'QA', %s, 'CBSE', %s) RETURNING id",
                        (str(uuid.uuid4()), DATASET, f"CBSE Class {args.grade} Mathematics board papers with the "
                         "official marking-scheme answers", subject_id, args.grade))
            found = cur.fetchone()
        dataset_id = found[0]

        sql = ("INSERT INTO dataset_questions (id, dataset_id, chapter_id, topic_id, created_by, external_uid, format, "
               "question_type, bloom_level, text, options, answer, solution, marks, years_asked, source) "
               "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::question_type, %s::bloom_level, %s, %s, %s, %s, %s, %s, %s) "
               "ON CONFLICT (dataset_id, external_uid) DO UPDATE SET chapter_id = EXCLUDED.chapter_id, "
               "topic_id = EXCLUDED.topic_id, text = EXCLUDED.text, options = EXCLUDED.options, "
               "answer = EXCLUDED.answer, solution = EXCLUDED.solution, marks = EXCLUDED.marks, "
               "years_asked = EXCLUDED.years_asked, source = EXCLUDED.source "
               "WHERE dataset_questions.source->>'edited' IS NULL")
        for q in questions:
            mcq = TYPE[q["type"]] == "MCQ"
            years = sorted({int(p.split()[0]) for p in q["papers"]})
            cur.execute(sql, (
                str(uuid.uuid4()), dataset_id, chapter_ids[q["chapter"]], topic_ids.get(topic_ref(q)), created_by,
                q["uid"], q["type"].replace("_", "-"), TYPE[q["type"]], q["bloom_level"], q["text"],
                Json(q["options"]) if mcq else None, q["answer"], q["solution"], q["marks"], years,  # int[] column
                Json({"dataset": "cbse-board-papers", "papers": q["papers"]})))
        cur.execute("UPDATE datasets SET question_count = (SELECT count(*) FROM dataset_questions WHERE dataset_id = %s) "
                    "WHERE id = %s RETURNING question_count", (dataset_id, dataset_id))
        print(f"{DATASET}: {cur.fetchone()[0]} questions ({len(questions)} in this import)")

        cur.execute("INSERT INTO school_datasets (id, school_id, dataset_id, enabled) "
                    "SELECT gen_random_uuid(), sc.school_id, %s, true FROM school_curriculum sc "
                    "WHERE sc.subject_id = %s AND sc.enabled "
                    "AND NOT EXISTS (SELECT 1 FROM school_datasets d WHERE d.school_id = sc.school_id "
                    "AND d.dataset_id = %s)", (dataset_id, subject_id, dataset_id))
        print(f"switched on for {cur.rowcount} more school(s)")
    conn.close()


if __name__ == "__main__":
    main()
