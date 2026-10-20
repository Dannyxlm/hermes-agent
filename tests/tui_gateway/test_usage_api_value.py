"""API value is distinct from subscription billing; offline dated prices only."""
import pytest

from tui_gateway import server


def rpc(method, **params):
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def test_codex_api_value_is_not_included_zero():
    response = rpc("usage.estimate", provider="openai-codex", model="gpt-6.1-sol",
                   input_tokens=100000, output_tokens=10000, cache_read_tokens=50000)
    assert "result" in response, response
    value = response["result"]
    assert value["estimated_cost_usd"] == pytest.approx(0.305)
    assert value["status"] == "est"
    assert value["pricing_date"] and value["source_url"].startswith("https://developers.openai.com/")


def test_unknown_price_is_null_with_reason():
    response = rpc("usage.estimate", provider="openai-codex", model="fictional-unknown", input_tokens=1000)
    assert "result" in response, response
    assert response["result"]["estimated_cost_usd"] is None
    assert response["result"]["reason"]


def test_long_context_prices_the_whole_request():
    value = rpc("usage.estimate", provider="openai", model="gpt-6.1-sol",
                input_tokens=300000, output_tokens=10000)["result"]
    assert value["estimated_cost_usd"] == pytest.approx(1.35)


def test_unknown_cache_rate_and_negative_tokens():
    value = rpc("usage.estimate", provider="openai", model="gpt-5.3-codex", cache_write_tokens=1)["result"]
    assert value["estimated_cost_usd"] is None
    assert "cache" in value["reason"]
    assert "error" in rpc("usage.estimate", provider="openai", model="gpt-6.1-sol", input_tokens=-1)
