"""Real, KG-grounded question generation for Homework
(src/services/homework_content_resolver.py) - a chapter synced from KG
(has a `Chapter.order_index`) uses the real generator, one that hasn't
falls back to the deterministic placeholder, and a KG call that raises
falls back too rather than propagating. kg_client.generate_paper_questions
/ generate_replacement_candidates are monkeypatched here (a real call
against a running kg + Neo4j is verified separately); this only tests
Core's own routing/fallback logic. Same seeding pattern as
test_voice_test_reference_content.py.
"""

import uuid

from src.domain.models import BloomLevel, Chapter, School, SchoolClass, Subject, Topic
from src.services import homework_content_resolver


async def _seed_class_subject_chapter_topic(db_session, *, order_index: int | None):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    school_class = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Magnetic Effects", order_index=order_index)
    db_session.add_all([school_class, chapter])
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="Right-Hand Rule")
    db_session.add(topic)
    await db_session.flush()
    return school_class, subject, chapter, topic


_FAKE_KG_QUESTIONS = [
    {
        "text": "Why does a current-carrying wire deflect a compass needle?",
        "answer": "It produces a magnetic field around itself.",
        "bloom_level": "UNDERSTAND",
        "difficulty": "MEDIUM",
        "question_type": "SHORT_ANSWER",
        "chapter_number": 7,
        "topic": "Right-Hand Rule",
        "source_concept_ids": ["concept-1"],
    }
]


async def test_uses_the_kg_grounded_generator_when_the_chapter_has_real_content(db_session, monkeypatch):
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, order_index=7)

    captured = {}

    async def fake_generate_paper_questions(**kwargs):
        captured.update(kwargs)
        return _FAKE_KG_QUESTIONS

    monkeypatch.setattr(
        "src.services.homework_content_resolver.kg_client.generate_paper_questions",
        fake_generate_paper_questions,
    )

    questions = await homework_content_resolver.generate_questions(
        db_session,
        chapter_id=chapter.id,
        subject_id=subject.id,
        class_id=school_class.id,
        topic_id=topic.id,
        topic="Magnetic Effects",
        bloom_distribution={BloomLevel.UNDERSTAND: 1},
        count=1,
    )

    assert len(questions) == 1
    assert questions[0].text == _FAKE_KG_QUESTIONS[0]["text"]
    assert questions[0].bloom_level == BloomLevel.UNDERSTAND
    assert captured["board"] == "CBSE"
    assert captured["grade"] == 10
    assert captured["chapters"] == [{"number": 7, "name": "Magnetic Effects"}]
    assert captured["topic_names"] == ["Right-Hand Rule"]
    assert captured["bloom_distribution"] == {"UNDERSTAND": 1}


async def test_falls_back_to_deterministic_when_the_chapter_has_no_kg_content(db_session, monkeypatch):
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, order_index=None)

    called = False

    async def fake_generate_paper_questions(**kwargs):
        nonlocal called
        called = True
        return _FAKE_KG_QUESTIONS

    monkeypatch.setattr(
        "src.services.homework_content_resolver.kg_client.generate_paper_questions",
        fake_generate_paper_questions,
    )

    questions = await homework_content_resolver.generate_questions(
        db_session,
        chapter_id=chapter.id,
        subject_id=subject.id,
        class_id=school_class.id,
        topic_id=topic.id,
        topic="Magnetic Effects",
        bloom_distribution={BloomLevel.UNDERSTAND: 2},
        count=2,
    )

    assert called is False
    assert len(questions) == 2
    # DeterministicContentGenerator's clearly-templated placeholder shape.
    assert all("Magnetic Effects" in q.text for q in questions)


async def test_falls_back_to_deterministic_when_the_kg_call_raises(db_session, monkeypatch):
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, order_index=7)

    async def failing_generate_paper_questions(**kwargs):
        raise RuntimeError("kg service unreachable")

    monkeypatch.setattr(
        "src.services.homework_content_resolver.kg_client.generate_paper_questions",
        failing_generate_paper_questions,
    )

    questions = await homework_content_resolver.generate_questions(
        db_session,
        chapter_id=chapter.id,
        subject_id=subject.id,
        class_id=school_class.id,
        topic_id=topic.id,
        topic="Magnetic Effects",
        bloom_distribution={BloomLevel.UNDERSTAND: 1},
        count=1,
    )

    assert len(questions) == 1
    assert "Magnetic Effects" in questions[0].text


async def test_replacement_candidates_use_the_kg_grounded_generator_when_available(db_session, monkeypatch):
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, order_index=7)

    async def fake_generate_replacement_candidates(**kwargs):
        return [{"text": "A rephrased grounded question?", "answer": "...", "bloom_level": "UNDERSTAND"}]

    monkeypatch.setattr(
        "src.services.homework_content_resolver.kg_client.generate_replacement_candidates",
        fake_generate_replacement_candidates,
    )

    candidates = await homework_content_resolver.generate_replacement_candidates(
        db_session,
        chapter_id=chapter.id,
        subject_id=subject.id,
        class_id=school_class.id,
        topic_id=topic.id,
        topic="Magnetic Effects",
        bloom_level=BloomLevel.UNDERSTAND,
        exclude_text="old question",
        count=1,
    )

    assert candidates == ["A rephrased grounded question?"]


async def test_replacement_candidates_fall_back_to_deterministic_without_kg_content(db_session, monkeypatch):
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(db_session, order_index=None)

    called = False

    async def fake_generate_replacement_candidates(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        "src.services.homework_content_resolver.kg_client.generate_replacement_candidates",
        fake_generate_replacement_candidates,
    )

    candidates = await homework_content_resolver.generate_replacement_candidates(
        db_session,
        chapter_id=chapter.id,
        subject_id=subject.id,
        class_id=school_class.id,
        topic_id=topic.id,
        topic="Magnetic Effects",
        bloom_level=BloomLevel.UNDERSTAND,
        exclude_text="old question",
        count=1,
    )

    assert called is False
    assert len(candidates) == 1
