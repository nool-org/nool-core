"""HTTP client for nool-kg (FalkorDB-backed textbook graph, deployed at
https://nool-data.draco-labs.in) - this service's only data source now that
Neo4j/nool-data-ingestion is out of scope. Read queries are ported from
neo4j_client.py's Cypher shapes but hit nool-kg's existing REST API instead
of a graph driver; nool-kg itself is never modified.

nool-kg has no Curriculum/Concept/Definition/Formula/Figure layer - only
Chapter -> Topic -> Subtopic, each carrying its own text_content plus
activities/images/examples/questions. Grounding context for question
generation is therefore built from that text_content, not extracted
concepts; see generator.py for the corresponding prompt/schema changes.
"""

from __future__ import annotations

import re

import httpx

_client: httpx.AsyncClient | None = None


def init_client(base_url: str) -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
    return _client


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("nool-kg client not initialized. Call init_client() first.")
    return _client


async def dispose_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def check_connection() -> bool:
    """Used by /ready. Never raises - a failed check just means "not ready"."""
    try:
        response = await get_client().get("/")
        return response.status_code < 500
    except httpx.HTTPError:
        return False


async def list_subjects() -> list[str]:
    """The graph names nool-kg actually has. Checked before ever querying a
    specific graph by name (see get_curriculum_tree/get_concept_context_for_scope)
    - nool-kg's own FalkorDB backend silently creates an empty graph the
    first time *anything* queries a name that doesn't exist yet (confirmed
    live: a bad graph name shows up in this same list afterward, forever,
    with no delete endpoint exposed to undo it) - so a wrong/typo'd
    board+grade+subject combination must never reach the chapters/topics
    endpoints at all.
    """
    response = await get_client().get("/api/subjects")
    response.raise_for_status()
    return response.json()


_GRAPH_NAME_PATTERN = re.compile(r"^(?P<subject>.+)_Grade_(?P<grade>\d+)_NCERT$")


async def list_curriculum_subjects() -> list[dict]:
    """Real, browsable curriculum content in nool-kg - `list_subjects()`
    filtered down to graphs matching the "{subject}_Grade_{grade}_NCERT"
    naming convention, and further to only those with at least one chapter
    (list_subjects() itself can include stray empty graphs a past typo'd
    lookup accidentally created - see that function's docstring; this list
    is what a picker should actually offer, not the raw graph names).
    Returns [{"name": <bare subject>, "grade": <int>, "chapter_count": <int>}, ...].
    """
    client = get_client()
    candidates = []
    for graph in await list_subjects():
        match = _GRAPH_NAME_PATTERN.match(graph)
        if match:
            candidates.append((graph, match.group("subject"), int(match.group("grade"))))

    results = []
    for graph, subject, grade in candidates:
        chapters_response = await client.get(f"/api/subjects/{graph}/chapters")
        chapters = chapters_response.json() if chapters_response.status_code == 200 else []
        if chapters:
            results.append({"name": subject, "grade": grade, "chapter_count": len(chapters)})
    return results


def graph_name_for(*, board: str, grade: int, subject: str) -> str:
    """nool-kg's own naming convention for a subject graph, e.g.
    'Science_Grade_10_NCERT'. `board` isn't part of nool-kg's graph naming
    today (it only ever ingested CBSE/NCERT content) - accepted here for a
    stable call signature if that ever changes, not used yet.
    """
    return f"{subject}_Grade_{grade}_NCERT"


_LEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)")


def _topic_number(title: str, fallback_index: int) -> str:
    """nool-kg topic/subtopic titles are usually prefixed with their own
    numbering (e.g. '1.2 TYPES OF CHEMICAL REACTIONS') - reuse it as the
    TopicTreeOut.number so chapter-intro topics without one (e.g.
    'Introduction') still get a stable, order-preserving fallback.
    """
    match = _LEADING_NUMBER.match(title.strip())
    return match.group(1) if match else str(fallback_index)


async def get_curriculum_tree(*, board: str = "CBSE", grade: int = 10, subject: str = "Science") -> dict | None:
    graph = graph_name_for(board=board, grade=grade, subject=subject)
    if graph not in await list_subjects():
        return None
    client = get_client()

    chapters_response = await client.get(f"/api/subjects/{graph}/chapters")
    chapters_response.raise_for_status()
    chapters = chapters_response.json()
    if not chapters:
        return None

    # One call for every subtopic in the graph, grouped by parent topic
    # title - nool-kg's /subtopics endpoint identifies the parent by title
    # text (topic_title), not id, and only ever needs fetching once here
    # rather than per-topic.
    subtopics_response = await client.get(f"/api/subjects/{graph}/subtopics")
    subtopics_by_topic_title: dict[str, list[dict]] = {}
    if subtopics_response.status_code == 200:
        for s in subtopics_response.json():
            subtopics_by_topic_title.setdefault(s["topic_title"], []).append(s)

    tree_chapters = []
    for chapter in chapters:
        topics_response = await client.get(
            f"/api/subjects/{graph}/chapters/{chapter['id']}/topics"
        )
        topics = topics_response.json() if topics_response.status_code == 200 else []
        tree_chapters.append(
            {
                "ref": chapter["id"],
                "number": chapter["number"],
                "name": chapter["title"],
                "topics": [
                    {
                        "ref": t["id"],
                        "number": _topic_number(t["title"], idx),
                        "name": t["title"],
                        "subtopics": [
                            {"ref": s["id"], "number": _topic_number(s["title"], sidx), "name": s["title"]}
                            for sidx, s in enumerate(subtopics_by_topic_title.get(t["title"], []), start=1)
                        ],
                    }
                    for idx, t in enumerate(topics, start=1)
                ],
            }
        )

    return {"board": board, "grade": grade, "subject": subject, "chapters": tree_chapters}


async def get_concept_context_for_scope(
    *, chapter_numbers: list[int], topic_names: list[str] | None = None, subject: str = "Science",
    board: str = "CBSE", grade: int = 10,
) -> list[dict]:
    """Grounding context for question generation: each topic (optionally
    narrowed to topic_names) in the requested chapters, with its own
    text_content plus activities/examples/questions pulled via nool-kg's
    /content endpoint. Shaped to match generator.py's expectations, just
    without the concept-level fields (definitions/formulas/figures) nool-kg
    doesn't have.
    """
    graph = graph_name_for(board=board, grade=grade, subject=subject)
    if graph not in await list_subjects():
        return []
    client = get_client()

    chapters_response = await client.get(f"/api/subjects/{graph}/chapters")
    if chapters_response.status_code != 200:
        return []
    chapters = {c["number"]: c for c in chapters_response.json() if c["number"] in chapter_numbers}

    # Core's synced topic names can differ in case from nool-kg's titles
    # (e.g. "1.2 TYPES OF CHEMICAL REACTIONS" vs "1.2 Types of Chemical Reactions").
    wanted = {name.strip().casefold() for name in topic_names or []}
    context: list[dict] = []
    for chapter_number, chapter in chapters.items():
        topics_response = await client.get(f"/api/subjects/{graph}/chapters/{chapter['id']}/topics")
        if topics_response.status_code != 200:
            continue
        for topic in topics_response.json():
            if wanted and topic["title"].strip().casefold() not in wanted:
                continue
            content_response = await client.get(
                f"/api/subjects/{graph}/content", params={"title_or_id": topic["id"]}
            )
            content = content_response.json() if content_response.status_code == 200 else {}
            context.append(
                {
                    "chapter_number": chapter_number,
                    "chapter_title": chapter["title"],
                    "topic": topic["title"],
                    "topic_ref": topic["id"],
                    "text_content": topic.get("text_content", ""),
                    "activities": [a.get("description", "") for a in content.get("activities", [])],
                    "examples": [e.get("content", "") for e in content.get("examples", [])],
                    "textbook_questions": [q.get("text", "") for q in content.get("questions", [])],
                }
            )
    return context
