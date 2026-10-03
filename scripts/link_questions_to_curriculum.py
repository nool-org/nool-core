"""Attach every dataset_question to the topic/subtopic of the textbook it
assesses (dataset_questions.topic_id / subtopic_id), so homework and paper
generation can select questions by curriculum relation instead of search.

Per chapter, the candidate "sections" are each topic's own intro text and
each subtopic. A question's embedding (question_embeddings) shortlists the
closest sections; gpt-5-mini then picks the one it actually assesses - or
none. Works per external_uid (every Dataset copy of a question gets the
same link) and records how it was linked in source->'enriched'->'topic'.
Only fills questions that have no topic yet; re-run safely.

Section text comes from nool-kg's JSON export (chapters 2+), and for
chapter 1 from its original NB002 JSON, indexed exactly like NB005 did -
matching the CH01_* ids already synced into this database.

Usage (host):
    python scripts/link_questions_to_curriculum.py --kg C:/pet/nool-kg --limit 60   # trial
    python scripts/link_questions_to_curriculum.py --kg C:/pet/nool-kg
    # retry the leftovers against EVERY chapter (fixes source chapter mistakes; records why if nothing fits)
    python scripts/link_questions_to_curriculum.py --kg C:/pet/nool-kg --anywhere --model gpt-5.1 --reasoning medium
"""
import argparse
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import psycopg2
from llama_index.embeddings.openai import OpenAIEmbedding
from openai import OpenAI

from import_dataset import db_config, read_env

EMBED_MODEL = "text-embedding-3-small"  # must match question_embeddings
LLM = "gpt-5-mini"
SHORTLIST = 4
BATCH = 15


def sections_from_export(doc):
    for t in doc["chapter"]["topics"]:
        yield from _sections(t["id"], t["title"], t["text_content"], t["questions"], t["activities"],
                             [(s["id"], s["title"], s["text_content"], s["questions"], s["activities"]) for s in t["subtopics"]])


def sections_from_legacy_ch1(doc):
    """NB005's own indexing of the original chapter-1 JSON (Topic[i] -> CH01_T0i, Subtopic[j] -> _S0j)."""
    for ti, t in enumerate(doc["Subject"]["Grade"]["Chapter"]["Topic"], 1):
        subs = t.get("Subtopic", [])
        subs = [subs] if isinstance(subs, dict) else subs
        tid = f"CH01_T{ti:02d}"
        yield from _sections(tid, t.get("title", ""), t.get("text_content", ""), t.get("questions", []),
                             t.get("activities", []),
                             [(f"{tid}_S{si:02d}", s.get("title", ""), s.get("text_content", ""), s.get("questions", []),
                               s.get("activities", [])) for si, s in enumerate(subs, 1)])


def _sections(topic_ref, title, text, questions, activities, subtopics):
    def body(t, x, qs, acts):
        qtext = " ".join((q.get("question_text") or q.get("text") or "") for q in qs)
        atext = " ".join(a.get("title", "") for a in acts)
        return f"{t}\n{x}\n{atext}\n{qtext}"[:6000]

    if text.strip() or not subtopics:
        yield {"topic_ref": topic_ref, "subtopic_ref": None, "title": title, "text": body(title, text, questions, activities)}
    for ref, st, sx, sq, sa in subtopics:
        yield {"topic_ref": topic_ref, "subtopic_ref": ref, "title": f"{title} > {st}", "text": body(st, sx, sq, sa)}


SYSTEM = """You map CBSE Class 10 Science questions to the textbook section they assess.
For each question you get its chapter's numbered candidate sections (most similar first).
Pick the ONE section whose content the question mainly tests, answering with its number key.
If you answer "none", give a short reason (e.g. "topic not in the current NCERT syllabus: periodic table"),
otherwise reason may be empty. Prefer the most specific section
(a subsection over its parent's intro). Answer "none" only if no section of this chapter fits.
confidence: high = clearly that section; medium = plausible, could be a neighbour; low = a guess."""

SCHEMA = {"name": "links", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "section", "confidence", "reason"],
        "properties": {"id": {"type": "string"}, "section": {"type": "string"}, "reason": {"type": "string"},
                       "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}}}}}


# external_uids repeat across subjects (every graph starts at CH01): writes stay in this subject's catalog
CATALOG = " AND dataset_id IN (SELECT id FROM datasets WHERE subject_id = %s AND school_id IS NULL)"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True, type=Path, help="nool-kg repo (output/ + notebooks/)")
    ap.add_argument("--export", default="output/Class10_Science")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--subject-name", default="Science")
    ap.add_argument("--grade", type=int, default=10)
    ap.add_argument("--anywhere", action="store_true", help="candidates from all chapters, not only the source chapter")
    ap.add_argument("--model", default=LLM)
    ap.add_argument("--reasoning", default="minimal", choices=["minimal", "low", "medium", "high"])
    args = ap.parse_args()

    env_file = Path(__file__).resolve().parent.parent / ".env"
    key = os.environ.get("OPENAI_API_KEY") or read_env(env_file).get("OPENAI_API_KEY")
    llm, embed = OpenAI(api_key=key), OpenAIEmbedding(model=EMBED_MODEL, api_key=key, embed_batch_size=100)

    sections = defaultdict(list)  # chapter kg_ref -> sections
    for n in range(1, 14):
        if n == 1:
            doc = json.loads((args.kg / "notebooks/10_Science_1,json").read_text(encoding="utf-8"))
            sections["CH01"] = list(sections_from_legacy_ch1(doc))
            chapter_title = doc["Subject"]["Grade"]["Chapter"]["title"]
        else:
            doc = json.loads(next((args.kg / args.export).glob(f"ch{n:02d}_*.json")).read_text(encoding="utf-8"))
            sections[f"CH{n:02d}"] = list(sections_from_export(doc))
            chapter_title = doc["chapter"]["title"]
        for sec in sections[f"CH{n:02d}"]:
            sec["chapter"] = f"CH{n:02d}"
            sec["chapter_title"] = chapter_title
    flat = [s for secs in sections.values() for s in secs]
    vectors = np.array(embed.get_text_embedding_batch([s["text"] for s in flat]))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    for s, v in zip(flat, vectors):
        s["vec"] = v
    print(f"{len(flat)} sections embedded across {len(sections)} chapters")
    if args.anywhere:  # one pool of every section, titles prefixed with their chapter
        sections = {"ALL": [{**sec, "title": f"Ch {sec['chapter'][2:]} {sec['chapter_title']}: {sec['title']}"}
                            for sec in flat]}
    shortlist = 8 if args.anywhere else SHORTLIST

    conn = psycopg2.connect(**db_config(env_file))
    with conn.cursor() as cur:
        # kg_refs are only unique within one subject (every grade's graph starts at CH01)
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = %s", (args.subject_name, args.grade))
        subject_id = cur.fetchone()[0]
        cur.execute("SELECT t.kg_ref, t.id FROM topics t JOIN chapters c ON c.id = t.chapter_id "
                    "WHERE c.subject_id = %s AND t.kg_ref IS NOT NULL", (subject_id,))
        topic_ids = dict(cur.fetchall())
        cur.execute("SELECT s.kg_ref, s.id FROM subtopics s JOIN topics t ON t.id = s.topic_id "
                    "JOIN chapters c ON c.id = t.chapter_id WHERE c.subject_id = %s AND s.kg_ref IS NOT NULL",
                    (subject_id,))
        subtopic_ids = dict(cur.fetchall())
        missing = {s["topic_ref"] for s in flat} - topic_ids.keys() | {s["subtopic_ref"] for s in flat if s["subtopic_ref"]} - subtopic_ids.keys()
        assert not missing, f"sections not synced into this DB yet: {sorted(missing)[:10]}"
        cur.execute(
            "SELECT DISTINCT ON (q.external_uid) q.external_uid, c.kg_ref, q.text, q.answer, e.embedding::text "
            "FROM dataset_questions q JOIN chapters c ON c.id = q.chapter_id "
            "JOIN question_embeddings e ON e.dataset_question_id = q.id "
            "WHERE q.topic_id IS NULL AND q.external_uid IS NOT NULL AND c.subject_id = %s "
            "AND q.dataset_id IN (SELECT id FROM datasets WHERE subject_id = c.subject_id AND school_id IS NULL) "
            # already judged "fits no section" (e.g. out of syllabus): only an --anywhere retry revisits them
            + ("" if args.anywhere else "AND NOT (COALESCE(q.source->'enriched', '{}') ? 'topic_none_reason') ")
            + "ORDER BY q.external_uid", (subject_id,))
        todo = cur.fetchall()[:args.limit]
    print(f"{len(todo)} questions to link")

    # Shortlist by cosine similarity within the question's own chapter.
    by_chapter = defaultdict(list)
    for uid, ch, text, answer, emb in todo:
        orig_ch = ch
        ch = "ALL" if args.anywhere else ch
        secs = sections[ch]
        sims = np.array([s["vec"] for s in secs]) @ np.array(json.loads(emb))
        order = np.argsort(-sims)
        by_chapter[ch].append({"uid": uid, "chapter": orig_ch, "text": text[:900], "answer": (answer or "")[:300],
                               "ranked": [(int(i), float(sims[i])) for i in order]})

    def link(ch, batch):
        secs = sections[ch]
        payload = [{"id": q["uid"], "question": q["text"], "answer": q["answer"],
                    "candidates": {str(i): secs[i]["title"] for i, _ in q["ranked"][:shortlist]}} for q in batch]
        catalog = {str(i): s["title"] for i, s in enumerate(secs)}
        # enum of this chapter's section keys: the model can only answer a valid key (or "none")
        schema = json.loads(json.dumps(SCHEMA))
        schema["schema"]["properties"]["results"]["items"]["properties"]["section"] = {
            "type": "string", "enum": [*catalog, "none"], "description": "the section's number key, or none"}
        r = llm.chat.completions.create(
            model=args.model, reasoning_effort=args.reasoning, response_format={"type": "json_schema", "json_schema": schema},
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps({"all_sections": catalog, "questions": payload},
                                                             ensure_ascii=False)}])
        return ch, batch, {x["id"]: x for x in json.loads(r.choices[0].message.content)["results"]}

    jobs = [(ch, qs[i:i + BATCH]) for ch, qs in by_chapter.items() for i in range(0, len(qs), BATCH)]
    stats = Counter()
    with ThreadPoolExecutor(args.workers) as pool:
        for future in [pool.submit(link, ch, b) for ch, b in jobs]:
            try:
                ch, batch, picks = future.result()
            except Exception as e:
                stats["failed_batches"] += 1
                print("  batch failed:", type(e).__name__, str(e)[:120])
                continue
            with conn, conn.cursor() as cur:
                for q in batch:
                    p = picks.get(q["uid"])
                    if not p or not p["section"].isdigit() or int(p["section"]) >= len(sections[ch]):
                        stats["none_or_invalid"] += 1
                        if p and p.get("reason"):  # keep the why, so "unlinked" is explained, not silent
                            cur.execute(
                                "UPDATE dataset_questions SET source = jsonb_set(COALESCE(source, '{}'), '{enriched}', "
                                "COALESCE(source->'enriched', '{}') || %s::jsonb) WHERE external_uid = %s" + CATALOG,
                                (json.dumps({"topic_none_reason": f"{args.model}: {p['reason']}"}), q["uid"], subject_id))
                        continue
                    idx = int(p["section"])
                    s = sections[ch][idx]
                    rank = next(r for r, (i, _) in enumerate(q["ranked"]) if i == idx)
                    stats[f"conf_{p['confidence']}"] += 1
                    stats["agrees_with_embedding_top1" if rank == 0 else "llm_moved_off_top1"] += 1
                    moved = s["chapter"] != q["chapter"]
                    stats["chapter_corrected"] += moved
                    cur.execute(
                        "UPDATE dataset_questions SET topic_id = %s, subtopic_id = %s, "
                        "chapter_id = (SELECT id FROM chapters WHERE kg_ref = %s AND subject_id = %s), "
                        "source = jsonb_set(COALESCE(source, '{}'), '{enriched}', COALESCE(source->'enriched', '{}') || %s::jsonb) "
                        "WHERE external_uid = %s AND topic_id IS NULL" + CATALOG,
                        (topic_ids[s["topic_ref"]], subtopic_ids.get(s["subtopic_ref"]), s["chapter"], subject_id,
                         json.dumps({"topic": f"emb+llm:{args.model}", "topic_confidence": p["confidence"],
                                     "topic_similarity": round(q["ranked"][rank][1], 3),
                                     **({"chapter_corrected_from": q["chapter"]} if moved else {})}), q["uid"], subject_id))
                    stats["linked"] += 1
            if stats["linked"] and stats["linked"] % 1000 < BATCH:
                print(f"  linked {stats['linked']}/{len(todo)}", flush=True)
    conn.close()
    print(dict(stats))


if __name__ == "__main__":
    main()
