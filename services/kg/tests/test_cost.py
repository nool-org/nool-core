"""kg's model calls are costed and summed per request (X-Nool-Cost) like generation's."""

from types import SimpleNamespace

from src.services import cost


def test_a_chat_call_is_costed_and_summed_for_the_request():
    spend = cost.new_spend()
    token = cost.spent.set(spend)
    try:
        usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=100_000,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=200_000))
        cost.log_chat_usage("gpt-5.1", usage)
        cost.log_chat_usage("gpt-5.1", None)  # no usage reported: nothing added
    finally:
        cost.spent.reset(token)
    # 800k fresh * 1.25 + 200k cached * 0.125 + 100k out * 10.00, per million
    assert spend["calls"] == 1 and spend["models"] == ["gpt-5.1"]
    assert round(spend["usd"], 4) == round(1.0 + 0.025 + 1.0, 4)
    assert '"usd":2.025' in cost.cost_header(spend)
