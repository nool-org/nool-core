"""Smoke check for nool_kg_client - hits the real, already-deployed nool-kg
instance (https://nool-data.draco-labs.in) rather than mocking it, since
that live service (not a local fixture) is this module's only real
contract. Skips if it's unreachable rather than failing CI on a network
blip.
"""

import pytest
import pytest_asyncio

from src.services import nool_kg_client


def test_topic_number_prefers_the_textbook_numbering():
    assert nool_kg_client._topic_number("1.2 TYPES OF CHEMICAL REACTIONS", fallback_index=3) == "1.2"
    assert nool_kg_client._topic_number("Introduction", fallback_index=1) == "1"


def test_graph_name_for_matches_nool_kgs_naming_convention():
    assert nool_kg_client.graph_name_for(board="CBSE", grade=10, subject="Science") == "Science_Grade_10_NCERT"


def test_graph_name_pattern_parses_subject_and_grade_back_out():
    match = nool_kg_client._GRAPH_NAME_PATTERN.match("Science_Grade_10_NCERT")
    assert match is not None
    assert match.group("subject") == "Science"
    assert match.group("grade") == "10"

    # A subject name that itself contains "_Grade_" text is still parsed
    # by matching the *last* "_Grade_{digits}_NCERT" suffix, since `.+` is
    # greedy - not something to rely on, but confirms real names sync fine.
    assert nool_kg_client._GRAPH_NAME_PATTERN.match("Physical Science_Grade_9_NCERT").group("subject") == (
        "Physical Science"
    )


@pytest_asyncio.fixture
async def live_client():
    nool_kg_client.init_client("https://nool-data.draco-labs.in")
    try:
        if not await nool_kg_client.check_connection():
            pytest.skip("nool-data.draco-labs.in is unreachable")
        yield
    finally:
        await nool_kg_client.dispose_client()


@pytest.mark.asyncio
async def test_get_curriculum_tree_returns_the_real_chapter_one(live_client):
    tree = await nool_kg_client.get_curriculum_tree(board="CBSE", grade=10, subject="Science")
    assert tree is not None
    chapter = next(c for c in tree["chapters"] if c["ref"] == "CH01")
    assert chapter["name"] == "Chemical Reactions and Equations"
    assert any(t["name"].startswith("1.2") for t in chapter["topics"])
    topic_1_2 = next(t for t in chapter["topics"] if t["name"].startswith("1.2"))
    assert any(s["name"].startswith("1.2.1") for s in topic_1_2["subtopics"])


@pytest.mark.asyncio
async def test_get_concept_context_for_scope_returns_real_topic_text(live_client):
    context = await nool_kg_client.get_concept_context_for_scope(
        chapter_numbers=[1], subject="Science", board="CBSE", grade=10
    )
    assert context
    assert all(c["text_content"] or c["activities"] or c["examples"] for c in context)


@pytest.mark.asyncio
async def test_get_concept_context_for_scope_matches_topic_names_ignoring_case(live_client):
    """Core stores synced topic names upper-cased ("1.2 TYPES OF CHEMICAL
    REACTIONS"); nool-kg's title is "1.2 Types of Chemical Reactions"."""
    context = await nool_kg_client.get_concept_context_for_scope(
        chapter_numbers=[1], topic_names=["1.2 TYPES OF CHEMICAL REACTIONS"], subject="Science", board="CBSE", grade=10
    )
    assert [c["topic_ref"] for c in context] == ["CH01_T02"]


@pytest.mark.asyncio
async def test_list_curriculum_subjects_includes_science_and_excludes_empty_stray_graphs(live_client):
    """Also a regression check on the stray graphs a past typo'd lookup
    left behind (e.g. "Class 10 Science_Grade_10_NCERT", empty) - these
    match the naming pattern but have zero chapters, so a picker built on
    this list never surfaces them.
    """
    subjects = await nool_kg_client.list_curriculum_subjects()
    names = {s["name"] for s in subjects}
    assert "Science" in names
    assert "Class 10 Science" not in names
    science = next(s for s in subjects if s["name"] == "Science")
    assert science["grade"] == 10
    assert science["chapter_count"] >= 1
