"""One reply at a time (_send_response): a reply asked for while Milo is
mid-reply waits for response.done instead of being refused by the API (which
left the student stuck on "thinking")."""

import asyncio

from src.services import realtime_service as rs
from test_realtime_limits import FakeWS


def test_send_response_queues_then_flushes_on_done():
    async def run():
        ws = FakeWS([])
        call = rs.RealtimeCall(call_id="c", owner_ref="o", state=None)
        await rs._send_response(call, ws, '{"type": "response.create"}')
        await rs._send_response(call, ws, '{"type": "response.create", "n": 2}')
        assert len(ws.sent) == 1 and call.pending_response is not None  # the second waits
        return call, ws

    call, ws = asyncio.run(run())
    assert ws.sent == [{"type": "response.create"}]


def test_a_stale_in_progress_flag_does_not_block_forever(monkeypatch):
    async def run():
        ws = FakeWS([])
        call = rs.RealtimeCall(call_id="c", owner_ref="o", state=None)
        call.response_active = True
        call.response_since = asyncio.get_running_loop().time() - rs.RESPONSE_STALE_SECONDS - 1
        await rs._send_response(call, ws, '{"type": "response.create"}')
        return ws

    assert asyncio.run(run()).sent == [{"type": "response.create"}]
