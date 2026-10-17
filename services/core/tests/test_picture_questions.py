"""Picture questions: a drawn (inline SVG) figure reaches the screens that draw
it; an imported image file never does, and text-only callers see neither."""
import uuid

import pytest
from sqlalchemy import select

from src.domain.models import BloomLevel, DatasetQuestion, QuestionType
from src.services.question_selector import figure_svgs, text_only_filter
from tests.test_daily_practice import _seed

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 240"><circle cx="9" cy="9" r="5"/></svg>'


async def _question(db, s, text, figures=None):
    q = DatasetQuestion(dataset_id=s.dataset.id, chapter_id=s.electricity.id, topic_id=s.current.id,
                        bloom_level=BloomLevel.APPLY, question_type=QuestionType.MCQ, text=text,
                        options=["1", "2", "3", "4"], answer="2", created_by=s.teacher.id,
                        external_uid=f"uid-{uuid.uuid4()}", **({"figures": figures} if figures else {}))
    db.add(q)
    await db.flush()
    return q


@pytest.mark.asyncio
async def test_drawn_pictures_only_where_asked(db_session):
    s = await _seed(db_session)
    plain = await _question(db_session, s, "What is 1 + 1?")
    drawn = await _question(db_session, s, "In the figure shown, find x.",
                            [{"kind": "diagram", "role": "question", "svg": SVG}])
    image = await _question(db_session, s, "Look at the image.", [{"kind": "diagram", "blob": "a/b.png"}])

    async def ids(**kw):
        return set((await db_session.execute(select(DatasetQuestion.id).where(
            DatasetQuestion.dataset_id == s.dataset.id, text_only_filter(**kw)))).scalars())

    assert await ids() == {plain.id}
    assert await ids(pictures=True) == {plain.id, drawn.id}
    assert image.id not in await ids(pictures=True)
    assert await figure_svgs(db_session, [plain.id, drawn.id, image.id, None]) == {drawn.id: SVG}
