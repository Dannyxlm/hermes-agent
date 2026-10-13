"""Deterministic, non-secret read of a checkout page's order summary for the payment approval card.

The vault tool runs ``ORDER_SUMMARY_JS`` over the supervised browser and passes the raw rows to
:func:`summarize_checkout`. Everything here is *page-sourced*: a page can lie about its own total, so the
card says where the number came from and the issuer's card limit stays the backstop. What this module
guarantees is narrower and checkable:

- the number the user sees comes from the page's own DOM, never from tool arguments the model wrote;
- nothing is guessed: an ambiguous or missing total is ``None`` ("Total not found"), and item lines are
  shown only when they add up to the subtotal or the total;
- the page never chooses the origin (the caller binds the approval to the browser's location).
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Rows: the deepest visible elements whose text holds a money amount plus some words (a label or a name).
# Same-origin top document only; a cross-origin iframe (Stripe Elements, some Shopify checkouts) is not
# readable and simply yields no rows, which surfaces as "Total not found".
ORDER_SUMMARY_JS = r"""
(() => {
  const __hermesOrderSummary = 1;
  const MONEY = /(?:US|CA|C|AU|A)?\$\s?\d|[€£]\s?\d|\b(?:USD|CAD|EUR|GBP|AUD)\b\s?\$?\s?\d|\d(?:[.,]\d+)*\s?(?:(?:USD|CAD|EUR|GBP|AUD)\b|[€£])/;
  const WORD = /[A-Za-z\u00C0-\u024F]{2,}/;
  const visible = (el) => {
    if (el.closest('[aria-hidden="true"], [hidden], script, style, noscript, template')) return false;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const qualifies = (el) => {
    const t = norm(el.innerText);
    return t.length > 0 && t.length <= 160 && MONEY.test(t) && WORD.test(t.replace(/\b(?:USD|CAD|EUR|GBP|AUD)\b/g, ''));
  };
  const rows = [];
  const seen = new Set();
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    if (rows.length >= 120) break;
    if (!qualifies(el) || !visible(el)) continue;
    if ([...el.children].some((c) => qualifies(c))) continue;
    const t = norm(el.innerText);
    if (seen.has(t)) continue;
    seen.add(t);
    rows.push(t);
  }
  const meta = (sel) => norm((document.querySelector(sel) || {}).content || '');
  let hint = meta('meta[itemprop="priceCurrency"]') || meta('meta[property="og:price:currency"]')
    || meta('meta[property="product:price:currency"]');
  try { hint = hint || ((window.Shopify || {}).currency || {}).active || ''; } catch (e) {}
  return JSON.stringify({
    rows,
    site_name: meta('meta[property="og:site_name"]'),
    app_name: meta('meta[name="application-name"]'),
    title: norm(document.title),
    currency_hint: String(hint || '').slice(0, 8),
  });
})()
"""

_CODES = ("USD", "CAD", "EUR", "GBP", "AUD")
_PREFIX = {"US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD", "€": "EUR", "£": "GBP"}
_SYMBOL = {"USD": "$", "CAD": "$", "AUD": "$", "EUR": "€", "GBP": "£"}
_MONEY_RE = re.compile(
    # Prefixed: "USD $32.77", "CAD 32.77", "C$32.77", "$32.77", "€9.50" (optional trailing code).
    r"(?:(?P<code1>\b(?:USD|CAD|EUR|GBP|AUD))\s?(?P<sym1>\$)?\s?|(?P<pre>US\$|CA\$|C\$|AU\$|A\$|\$|€|£)\s?)"
    # Capture the WHOLE numeric token: never recover a partial amount from bad punctuation.
    r"(?P<num1>\d+(?:[., \t\u00a0\u202f]\d+)*)(?![\d.,])(?:\s?(?P<code2>USD|CAD|EUR|GBP|AUD)\b)?"
    # Suffixed: "32.77 CAD", "9,50 €". No starting inside an invalid numeric token.
    r"|(?<![\w.,])(?P<num2>\d+(?:[., \t\u00a0\u202f]\d+)*)\s?(?:(?P<code3>USD|CAD|EUR|GBP|AUD)\b|(?P<sym2>€|£))")

_STRONG_TOTAL = re.compile(r"\b(order total|grand total|total due|amount due|total to pay|estimated total|"
                           r"total \(|total amount|you pay|total charge)", re.IGNORECASE)
_PLAIN_TOTAL = re.compile(r"\btotal\b", re.IGNORECASE)
_SUBTOTAL = re.compile(r"\bsub-?\s?total\b", re.IGNORECASE)
_ADJUST = (
    ("discount", re.compile(r"\b(discount|promo|coupon|savings|you save|gift card|credit applied)\b", re.IGNORECASE)),
    ("tax", re.compile(r"\b(tax|taxes|hst|gst|pst|qst|vat)\b", re.IGNORECASE)),
    ("shipping", re.compile(r"\b(shipping|delivery|postage|handling)\b", re.IGNORECASE)),
    ("fee", re.compile(r"\b(fee|fees|service charge)\b", re.IGNORECASE)),
)
_QTY = re.compile(r"(?:[×x]\s?(\d{1,3})\b|\b(?:qty|quantity)\s?:?\s?(\d{1,3})\b)", re.IGNORECASE)
_TITLE_NOISE = re.compile(r"^(checkout|secure checkout|payment|cart|order|review order|shopping cart)$", re.IGNORECASE)


def _currency_of(match: re.Match, hint: Optional[str]) -> Optional[str]:
    code = match.group("code1") or match.group("code2") or match.group("code3")
    if code:
        return code
    pre = match.group("pre") or match.group("sym2")
    if pre in _PREFIX:
        return _PREFIX[pre]
    hint = (hint or "").strip().upper()
    return hint if hint in _CODES else None


def _amount_minor(number: str) -> Optional[int]:
    """Accept integers, two-digit decimals and comma-grouped dot-decimals; reject ambiguity."""
    if re.fullmatch(r"\d+", number):
        return int(number) * 100
    if re.fullmatch(r"\d+[.,]\d{2}", number):
        whole, frac = re.split(r"[.,]", number)
        return int(whole) * 100 + int(frac)
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?", number):
        whole, _, frac = number.replace(",", "").partition(".")
        return int(whole) * 100 + int(frac or "00")
    return None


def parse_money(text: str, currency_hint: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The last money amount in *text*: ``{amount_minor, currency, display}`` or None."""
    matches = list(_MONEY_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    amount_minor = _amount_minor(m.group("num1") or m.group("num2"))
    if amount_minor is None:
        return None
    currency = _currency_of(m, currency_hint)
    symbol = _SYMBOL.get(currency or "", "$" if (m.group("pre") or "").endswith("$") or m.group("sym1") else "")
    major = f"{amount_minor // 100:,}.{amount_minor % 100:02d}"
    display = f"{symbol}{major}" if symbol else f"{major} {currency}" if currency else major
    return {"amount_minor": amount_minor, "currency": currency, "display": display}


def _label(text: str) -> str:
    stripped = _MONEY_RE.sub(" ", text)
    return re.sub(r"\s+", " ", stripped).strip(" :·-–—|")


def _merchant(raw: dict) -> Optional[dict[str, str]]:
    for key in ("site_name", "app_name"):
        name = str(raw.get(key) or "").strip()
        if name:
            return {"name": name[:60], "source": "page"}
    title = str(raw.get("title") or "").strip()
    parts = [p.strip() for p in re.split(r"\s[|–—-]\s", title) if p.strip()]
    parts = [p for p in parts if not _TITLE_NOISE.match(p)]
    return {"name": parts[-1][:60], "source": "page"} if parts else None


def _empty() -> dict[str, Any]:
    return {"merchant": None, "total": None, "lines": [], "lines_truncated": False, "adjustments": []}


def summarize_checkout(raw: Any) -> dict[str, Any]:
    """Classify raw rows into merchant, labelled total, verified item lines and adjustments."""
    if not isinstance(raw, dict):
        return _empty()
    hint = str(raw.get("currency_hint") or "")
    rows = [str(r) for r in raw.get("rows") or [] if isinstance(r, str)][:120]
    totals: list[tuple[int, dict, str]] = []
    subtotals: list[int] = []
    adjustments: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for row in rows:
        money = parse_money(row, hint)
        if money is None:
            continue
        label = _label(row)
        if _SUBTOTAL.search(label):
            subtotals.append(money["amount_minor"])
            continue
        kind = next((k for k, rx in _ADJUST if rx.search(label)), None)
        if kind:
            adjustments.append({"kind": kind, "amount_minor": money["amount_minor"]})
            continue
        if _STRONG_TOTAL.search(label):
            totals.append((2, money, label))
            continue
        if _PLAIN_TOTAL.search(label):
            totals.append((1, money, label))
            continue
        qty_match = _QTY.search(label)
        qty = int(qty_match.group(1) or qty_match.group(2)) if qty_match else 1
        name = re.sub(r"\s+", " ", _QTY.sub(" ", label)).strip(" :·-–—|")
        if len(re.findall(r"[^\W\d_]", name)) >= 2:
            items.append({"name": name[:80], "qty": qty, "amount_minor": money["amount_minor"]})

    out = _empty()
    out["merchant"] = _merchant(raw)
    total = _pick_total(totals)
    if total is not None:
        money, label = total
        out["total"] = {**money, "source": "page", "label": label[:40] or "Total"}
        signed = sum(-a["amount_minor"] if a["kind"] == "discount" else a["amount_minor"] for a in adjustments)
        line_sum = sum(i["amount_minor"] for i in items)
        if items and (line_sum + signed == money["amount_minor"] or line_sum in subtotals):
            out["lines"] = items[:8]
            out["lines_truncated"] = len(items) > 8
            out["adjustments"] = adjustments
        elif not items and adjustments:
            out["adjustments"] = adjustments
    return out


def _pick_total(totals: list[tuple[int, dict, str]]):
    if not totals:
        return None
    best = max(p for p, _, _ in totals)
    top = [(m, label) for p, m, label in totals if p == best]
    amounts = {m["amount_minor"] for m, _ in top}
    if len(amounts) == 1:
        return top[-1]
    last = top[-1]
    if last[0]["amount_minor"] == max(amounts):
        return last
    return None
