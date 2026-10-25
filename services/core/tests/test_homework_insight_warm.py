"""A just-built homework's insight is written in the background, once: an
open that lands while it's being written waits for that run, not a second."""
import asyncio
import contextlib

from src.api.routes import student_homework
from tests.test_topic_quiz import _homework_done, _seed, _start, _teach, _pools


async def test_insight_written_once_in_the_background(db_session, monkeypatch):
    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    hw = await _homework_done(db_session, s, (await _start(db_session, s, s.ohm)).id)
    calls, release = [], asyncio.Event()

    async def generate(**kw):
        calls.append(kw)
        await release.wait()
        return {"what_needs_understanding": "V = IR", "references": [], "key_idea_title": "Ohm",
                "key_idea_body": "b", "connection_prompt": "c"}

    class Proxy:  # the background run's own session -> the test's, commit = flush
        def __getattr__(self, name):
            return getattr(db_session, name)

        async def commit(self):
            await db_session.flush()

    @contextlib.asynccontextmanager
    async def own_session():
        yield Proxy()

    monkeypatch.setattr(student_homework, "generate_homework_insight", generate)
    monkeypatch.setattr("src.repositories.get_session", own_session)
    monkeypatch.setattr(db_session, "commit", db_session.flush)

    warm = asyncio.create_task(student_homework.warm_insight(hw.id, s.student.id, "tok"))
    await asyncio.sleep(0)  # the run has started and is waiting on the model
    opened = asyncio.create_task(student_homework._ensure_insight_generated(db_session, hw, s.student.id, "tok"))
    await asyncio.sleep(0)
    release.set()
    await warm
    assert (await opened)["what_needs_understanding"] == "V = IR"
    assert len(calls) == 1 and hw.insight_generated_at is not None
    await student_homework.warm_insight(hw.id, s.student.id, None)  # no token: nothing to do
    assert len(calls) == 1
