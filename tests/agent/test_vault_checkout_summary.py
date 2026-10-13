"""Deterministic checkout-summary extraction for the payment approval card.

The summary is read from the page (never from model-written tool arguments) and labelled page-sourced.
Anything ambiguous comes back as "not found" rather than a guess.
"""

from agent.vault_checkout_summary import parse_money, summarize_checkout


def _raw(rows, **kw):
    return {"rows": rows, "site_name": kw.get("site_name", ""), "app_name": "", "title": kw.get("title", ""),
            "currency_hint": kw.get("currency_hint", "")}


class TestParseMoney:
    def test_symbol_amount(self):
        assert parse_money("Total $32.77") == {"amount_minor": 3277, "currency": None, "display": "$32.77"}

    def test_code_wins_over_bare_dollar(self):
        assert parse_money("Order total USD $1,032.70")["currency"] == "USD"
        assert parse_money("Total 32.77 CAD")["amount_minor"] == 3277
        assert parse_money("Total C$32.77")["currency"] == "CAD"

    def test_unambiguous_symbols(self):
        assert parse_money("Total €9.50")["currency"] == "EUR"
        assert parse_money("Total £12")["amount_minor"] == 1200

    def test_hint_fills_a_bare_dollar(self):
        assert parse_money("Total $5.00", currency_hint="usd")["currency"] == "USD"

    def test_no_money(self):
        assert parse_money("Shipping calculated at next step") is None


class TestSummarizeCheckout:
    def test_harbor_type_example(self):
        out = summarize_checkout(_raw([
            "Plex Serif — Desktop licence ×1 $29.00",
            "Subtotal $29.00",
            "Tax $3.77",
            "Order total USD $32.77",
        ], site_name="Harbor Type Foundry", title="Checkout – Harbor Type"))
        assert out["merchant"] == {"name": "Harbor Type Foundry", "source": "page"}
        assert out["total"] == {"amount_minor": 3277, "currency": "USD", "display": "$32.77", "source": "page",
                                "label": "Order total"}
        assert out["lines"] == [{"name": "Plex Serif — Desktop licence", "qty": 1, "amount_minor": 2900}]
        assert out["adjustments"] == [{"kind": "tax", "amount_minor": 377}]
        assert out["lines_truncated"] is False

    def test_strong_label_beats_plain_total(self):
        out = summarize_checkout(_raw(["Total savings $5.00", "Total $40.00", "Amount due $35.00"]))
        assert out["total"]["amount_minor"] == 3500

    def test_conflicting_totals_are_not_guessed(self):
        out = summarize_checkout(_raw(["Total $40.00", "Item A $10.00", "Total $12.00"]))
        assert out["total"] is None

    def test_last_and_largest_agree(self):
        out = summarize_checkout(_raw(["Total $12.00", "Total $40.00"]))
        assert out["total"]["amount_minor"] == 4000

    def test_missing_total(self):
        out = summarize_checkout(_raw(["Plex Serif $29.00"]))
        assert out["total"] is None and out["lines"] == []

    def test_unverifiable_lines_are_dropped(self):
        # A recommendation carousel price does not add up to the total: show no items rather than wrong ones.
        out = summarize_checkout(_raw(["Plex Serif $29.00", "You may also like: Inter Display $49.00",
                                       "Tax $3.77", "Total $32.77"]))
        assert out["total"]["amount_minor"] == 3277
        assert out["lines"] == []

    def test_many_lines_truncate_at_eight(self):
        rows = [f"Font {i} $1.00" for i in range(10)] + ["Total $10.00"]
        out = summarize_checkout(_raw(rows))
        assert len(out["lines"]) == 8 and out["lines_truncated"] is True

    def test_merchant_falls_back_to_title(self):
        out = summarize_checkout(_raw(["Total $1.00"], title="Checkout | Harbor Type Foundry"))
        assert out["merchant"] == {"name": "Harbor Type Foundry", "source": "page"}

    def test_garbage_input_is_empty(self):
        out = summarize_checkout("not a dict")
        assert out == {"merchant": None, "total": None, "lines": [], "lines_truncated": False, "adjustments": []}
