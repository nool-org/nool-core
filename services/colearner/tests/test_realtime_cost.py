from src.services.cost_service import add_realtime_usage, add_transcription_usage, realtime_cost_report


def test_realtime_cost_splits_cached_and_sums_everything():
    totals: dict = {}
    # one response.done: 1000 text in (400 cached), 2000 audio in (1500 cached), 100 text out, 500 audio out
    add_realtime_usage(totals, {
        "input_token_details": {"text_tokens": 1000, "audio_tokens": 2000,
                                "cached_tokens_details": {"text_tokens": 400, "audio_tokens": 1500}},
        "output_token_details": {"text_tokens": 100, "audio_tokens": 500},
    })
    add_transcription_usage(totals, {"type": "duration", "seconds": 60})
    add_transcription_usage(totals, None)  # events without usage are ignored

    cost = realtime_cost_report(totals, "gpt-realtime-2.1-mini", "gpt-4o-mini-transcribe",
                                report_cost_usd=0.001, duration_seconds=90)

    assert totals["text_in"] == 600 and totals["audio_in"] == 500
    live = (600 * 0.60 + 400 * 0.06 + 100 * 2.40 + 500 * 10.00 + 1500 * 0.30 + 500 * 20.00) / 1_000_000
    assert abs(cost["breakdown_usd"]["live_call"] - live) < 1e-6
    assert cost["breakdown_usd"]["transcription"] == 0.003
    assert abs(cost["total_usd"] - (live + 0.003 + 0.001)) < 1e-6
