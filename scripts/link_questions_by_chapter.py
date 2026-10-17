"""Link a subject's bank questions to the textbook section they assess, when
its chapters are small enough to show the model every section (Maths: 1-4
topics a chapter) - no embedding shortlist needed. Same prompt, schema and
bookkeeping as link_questions_to_curriculum.py; the model runs through the
gateway, so any routed model works (e.g. gemini/gemini-3-flash-preview).
Only questions with no topic yet; re-run safely.

Usage (host):
    python scripts/link_questions_by_chapter.py --export C:/pet/nool-kg/output/Class10_Mathematics \
        --subject-name Mathematics --model gemini/gemini-3-flash-preview
"""
import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg2
from openai import OpenAI

from import_dataset import db_config, read_env
from link_questions_to_curriculum import BATCH, CATALOG, SCHEMA, SYSTEM

ENV = Path(__file__).resolve().parent.parent / ".env"


def sections(doc: dict) -> list[dict]:
    out = []
    for t in doc["chapter"]["topics"]:
        out.append({"topic_ref": t["id"], "subtopic_ref": None, "title": t["title"]})
        out += [{"topic_ref": t["id"], "subtopic_ref": s["id"], "title": f"{t['title']} > {s['title']}"}
                for s in t["subtopics"]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True, type=Path, help="nool-kg output/Class10_<Subject>")
    ap.add_argument("--subject-name", required=True)
    ap.add_argument("--grade", type=int, default=10)
    ap.add_argument("--model", default="gemini/gemini-3-flash-preview")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    env = read_env(ENV)
    llm = OpenAI(base_url="http://127.0.0.1:4000", api_key=env["LLM_GATEWAY_KEY_GENERATION"])

    by_ref = {}
    for path in sorted(args.export.glob("ch*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        by_ref[doc["chapter"]["id"]] = sections(doc)

    conn = psycopg2.connect(**db_config(ENV))
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = %s", (args.subject_name, args.grade))
        subject_id = cur.fetchone()[0]
        cur.execute("SELECT t.kg_ref, t.id FROM topics t JOIN chapters c ON c.id = t.chapter_id "
                    "WHERE c.subject_id = %s AND t.kg_ref IS NOT NULL", (subject_id,))
        topic_ids = dict(cur.fetchall())
        cur.execute("SELECT s.kg_ref, s.id FROM subtopics s JOIN topics t ON t.id = s.topic_id "
                    "JOIN chapters c ON c.id = t.chapter_id WHERE c.subject_id = %s", (subject_id,))
        subtopic_ids = dict(cur.fetchall())
        cur.execute(
            "SELECT DISTINCT ON (q.external_uid) q.external_uid, c.kg_ref, q.text, q.answer "
            "FROM dataset_questions q JOIN chapters c ON c.id = q.chapter_id "
            "WHERE q.topic_id IS NULL AND q.external_uid IS NOT NULL AND c.subject_id = %s" + CATALOG.replace(
                "dataset_id", "q.dataset_id") + " ORDER BY q.external_uid", (subject_id, subject_id))
        todo = cur.fetchall()
    print(f"{len(todo)} questions to link")

    by_chapter = defaultdict(list)
    for uid, ch, text, answer in todo:
        if ch in by_ref:
            by_chapter[ch].append({"uid": uid, "text": (text or "")[:900], "answer": (answer or "")[:300]})

    def link(ch, batch):
        secs = by_ref[ch]
        catalog = {str(i): s["title"] for i, s in enumerate(secs)}
        schema = json.loads(json.dumps(SCHEMA))
        schema["schema"]["properties"]["results"]["items"]["properties"]["section"] = {
            "type": "string", "enum": [*catalog, "none"], "description": "the section's number key, or none"}
        payload = [{"id": q["uid"], "question": q["text"], "answer": q["answer"], "candidates": catalog} for q in batch]
        r = llm.chat.completions.create(
            model=args.model, response_format={"type": "json_schema", "json_schema": schema},
            messages=[{"role": "system", "content": SYSTEM.replace("Science", args.subject_name)},
                      {"role": "user", "content": json.dumps({"all_sections": catalog, "questions": payload},
                                                             ensure_ascii=False)}],
            extra_body={"metadata": {"tags": ["feature:curriculum_linking", f"writer:{args.model}"]}})
        return ch, batch, {x["id"]: x for x in json.loads(r.choices[0].message.content)["results"]}

    jobs = [(ch, qs[i:i + BATCH]) for ch, qs in by_chapter.items() for i in range(0, len(qs), BATCH)]
    stats = Counter()
    with ThreadPoolExecutor(args.workers) as pool:
        for future in [pool.submit(link, ch, b) for ch, b in jobs]:
            try:
                ch, batch, picks = future.result()
            except Exception as e:
                stats["failed_batches"] += 1
                print("  batch failed:", type(e).__name__, str(e)[:160])
                continue
            with conn, conn.cursor() as cur:
                for q in batch:
                    p = picks.get(q["uid"])
                    if not p or not p["section"].isdigit() or int(p["section"]) >= len(by_ref[ch]):
                        stats["none"] += 1
                        if p and p.get("reason"):
                            cur.execute(
                                "UPDATE dataset_questions SET source = jsonb_set(COALESCE(source, '{}'), '{enriched}', "
                                "COALESCE(source->'enriched', '{}') || %s::jsonb) WHERE external_uid = %s" + CATALOG,
                                (json.dumps({"topic_none_reason": f"{args.model}: {p['reason']}"}), q["uid"], subject_id))
                        continue
                    s = by_ref[ch][int(p["section"])]
                    cur.execute(
                        "UPDATE dataset_questions SET topic_id = %s, subtopic_id = %s, source = jsonb_set("
                        "COALESCE(source, '{}'), '{enriched}', COALESCE(source->'enriched', '{}') || %s::jsonb) "
                        "WHERE external_uid = %s AND topic_id IS NULL" + CATALOG,
                        (topic_ids[s["topic_ref"]], subtopic_ids.get(s["subtopic_ref"]),
                         json.dumps({"topic": f"llm:{args.model}", "topic_confidence": p["confidence"]}),
                         q["uid"], subject_id))
                    stats["linked"] += 1
                    stats[f"conf_{p['confidence']}"] += 1
    conn.close()
    print(dict(stats))


if __name__ == "__main__":
    main()
