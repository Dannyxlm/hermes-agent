"""Money punctuation must never silently lower an approved checkout total."""

import pytest

from agent.vault_checkout_summary import parse_money, summarize_checkout


@pytest.mark.parametrize("amount", ["€9,50", "EUR 9,50", "9,50 EUR", "9,50 €"])
def test_decimal_comma_prefix_and_suffix(amount):
    assert parse_money(f"Order total {amount}") == {"amount_minor": 950, "currency": "EUR", "display": "€9.50"}
    out = summarize_checkout({"rows": [f"Order total {amount}"]})
    assert out["total"]["amount_minor"] == 950
    assert out["total"]["label"] == "Order total"


@pytest.mark.parametrize("amount", ["€9,5", "9,5 EUR", "€1.234", "1.234 EUR", "€1,23,4", "1,23,4 EUR"])
def test_ambiguous_or_malformed_punctuation_is_not_a_partial_amount(amount):
    assert parse_money(f"Order total {amount}") is None
    assert summarize_checkout({"rows": [f"Order total {amount}"]})["total"] is None
