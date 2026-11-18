"""Login / checkout form control classifier for vault autofill.

Python port (~170 LOC) of Merit-Systems/OpenInstinct's
``lib/manager/server/kernel-login-autofill.ts`` (MIT). Classifies visible
input controls on a page into login-autofill tokens. The vault fill path
uses the classification to select the single best current-password control
(the identifier is agent-visible metadata and is typed by the agent
itself via normal input tools).

Scoring:
- exact autocomplete-token match ................ 100
- type=password (not new/confirm/create/repeat) .. 90
- type=email / type=tel .......................... 85
- label/name regex heuristics .................. 70-75
Hard exclusions: autocomplete ``new-password`` / ``one-time-code``, and
label/name text matching ``(new|confirm|create|repeat)\\s*password``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import itertools

LOGIN_AUTOFILL_TOKENS = ("username", "email", "tel", "current-password")

# Payment / address autocomplete tokens (WHATWG) the checkout fill targets. ``cc-exp`` (combined
# MM/YY) is derived at fill time from exp_month + exp_year. Field-name/label heuristics below back
# up sites that omit autocomplete attributes.
PAYMENT_AUTOFILL_TOKENS = ("cc-number", "cc-name", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc")
ADDRESS_AUTOFILL_TOKENS = ("address-line1", "address-line2", "address-level2", "address-level1",
                           "postal-code", "country-name", "country")
_CHECKOUT_HEURISTICS = (
    (re.compile(r"\b(?:card\s*number|cardnumber|ccnumber|cc\s*num|pan)\b"), "cc-number"),
    (re.compile(r"\b(?:name\s*on\s*card|cardholder|cc\s*name|ccname)\b"), "cc-name"),
    (re.compile(r"\b(?:cvc|cvv|csc|security\s*code|card\s*code)\b"), "cc-csc"),
    (re.compile(r"\b(?:exp(?:iry|iration)?\s*month|exp\s*mm|ccmonth)\b"), "cc-exp-month"),
    (re.compile(r"\b(?:exp(?:iry|iration)?\s*year|exp\s*yy(?:yy)?|ccyear)\b"), "cc-exp-year"),
    (re.compile(r"\b(?:exp(?:iry|iration)?(?:\s*date)?|mm\s*yy|valid\s*thru)\b"), "cc-exp"),
    (re.compile(r"\b(?:address\s*(?:line\s*)?2|apt|suite|unit)\b"), "address-line2"),
    (re.compile(r"\b(?:address(?:\s*line\s*1)?|street)\b"), "address-line1"),
    (re.compile(r"\b(?:city|town|locality)\b"), "address-level2"),
    (re.compile(r"\b(?:state|province|region|county)\b"), "address-level1"),
    (re.compile(r"\b(?:zip|postal|postcode)\b"), "postal-code"),
    (re.compile(r"\b(?:country)\b"), "country-name"),
)

_EXCLUDED_AUTOCOMPLETE = {"new-password", "one-time-code"}

_RE_EXCLUDED_PASSWORD = re.compile(r"\b(?:new|confirm|create|repeat)\s*password\b")
_RE_EMAIL = re.compile(r"\b(?:e[\s-]?mail|email address)\b")
_RE_TEL = re.compile(r"\b(?:phone|telephone|mobile)\b")
_RE_USERNAME = re.compile(
    r"\b(?:user\s*name|username|login|account|member|membership|mileageplus)\b"
)


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


@dataclass(frozen=True)
class LoginControl:
    """Descriptor of a visible input control, as inspected in the page."""

    autocomplete: str
    form_index: Optional[int]
    index: int
    label: str
    name: str
    type: str
    max_length: Optional[int] = None
    # Class names of the control and its two nearest ancestors (plus their aria-label / data-testid): split
    # code widgets often carry no name, label or autocomplete, only ``class="otp-input"`` on a wrapper.
    hint: str = ""
    pattern: str = ""
    input_mode: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LoginControl:
        form_index = raw.get("formIndex", raw.get("form_index"))
        max_length = raw.get("maxLength", raw.get("max_length"))
        return cls(
            autocomplete=str(raw.get("autocomplete") or ""),
            form_index=int(form_index) if form_index is not None else None,
            index=int(raw.get("index") or 0),
            label=str(raw.get("label") or ""),
            name=str(raw.get("name") or ""),
            type=str(raw.get("type") or ""),
            max_length=int(max_length) if max_length is not None else None,
            hint=str(raw.get("hint") or ""),
            pattern=str(raw.get("pattern") or ""),
            input_mode=str(raw.get("inputMode", raw.get("input_mode")) or ""),
        )


@dataclass(frozen=True)
class ClassifiedLoginControl:
    control: LoginControl
    score: int
    token: str


def classify_login_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control, or return None if it is not a login fill target."""
    autocomplete_tokens = [
        t for t in control.autocomplete.lower().split() if t
    ]
    if any(t in _EXCLUDED_AUTOCOMPLETE for t in autocomplete_tokens):
        return None

    for token in LOGIN_AUTOFILL_TOKENS:
        if token in autocomplete_tokens:
            return ClassifiedLoginControl(control, 100, token)

    searchable = _normalize_text(
        " ".join(part for part in (control.name, control.label) if part)
    )
    if _RE_EXCLUDED_PASSWORD.search(searchable):
        return None
    if control.type == "password":
        return ClassifiedLoginControl(control, 90, "current-password")
    if control.type == "email":
        return ClassifiedLoginControl(control, 85, "email")
    if control.type == "tel":
        return ClassifiedLoginControl(control, 85, "tel")
    if _RE_EMAIL.search(searchable):
        return ClassifiedLoginControl(control, 75, "email")
    if _RE_TEL.search(searchable):
        return ClassifiedLoginControl(control, 75, "tel")
    if _RE_USERNAME.search(searchable):
        return ClassifiedLoginControl(control, 70, "username")
    return None


_RE_OTP = re.compile(
    r"\b(?:one[\s-]?time|verification|security|auth(?:entication|enticator)?|2fa|two[\s-]?factor|mfa|totp|otp|"
    r"passcode|sms)\b.*\b(?:code|pin|token)\b|\b(?:otp|totp|2fa|mfa|verification\s*code|passcode)\b"
)


_CODE_INPUT_TYPES = ("text", "tel", "number", "password", "")
# Split code widgets name themselves in class names more often than in labels ("ui-otp-field-input",
# "pin-input", "digit-box", "verification-code").
_RE_CODE_WIDGET = re.compile(r"\b(?:otp|totp|2fa|mfa|one\s*time|code|codes|pin|pincode|digit|digits|passcode|"
                             r"verification|verify)\b")
# A box group that names one of these is a date, phone, card or amount split into parts, never a code.
_RE_NOT_A_CODE = re.compile(r"\b(?:date|birth|birthday|dob|day|month|year|dd|mm|yy|yyyy|phone|telephone|mobile|"
                            r"zip|postal|postcode|card|cvv|cvc|ssn|sin|amount|price|qty|quantity)\b")
_RE_DIGIT_PATTERN = re.compile(r"^\^?(?:\[0-9\]|\\d)(?:[*+]|\{\d*,?\d*\})?\$?$")
_SPLIT_BOXES = range(4, 9)  # 4-8 single-character boxes: every code widget seen in the wild


def _words(*parts: str) -> str:
    """Attribute text as words: camelCase (``otpInput``) and letter-digit suffixes (``otp1``, ``dob3``) are
    split first, one direction only so ``2fa`` stays a word."""
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", " ".join(p for p in parts if p))
    return _normalize_text(re.sub(r"([A-Za-z])([0-9])", r"\1 \2", raw))


def _digit_only(c: LoginControl) -> bool:
    return (c.type in ("number", "tel") or c.input_mode.lower() == "numeric"
            or bool(_RE_DIGIT_PATTERN.match(c.pattern.strip())))


def _box_runs(controls: list[LoginControl]) -> list[list[LoginControl]]:
    """Maximal runs of single-character inputs adjacent in DOM order (no other input between them) and in
    the same form: the shape of an N-box code widget."""
    runs: list[list[LoginControl]] = []
    for c in sorted((c for c in controls if c.max_length == 1 and c.type in _CODE_INPUT_TYPES),
                    key=lambda c: c.index):
        last = runs[-1][-1] if runs else None
        if last is not None and c.index - last.index == 1 and c.form_index == last.form_index:
            runs[-1].append(c)
        else:
            runs.append([c])
    return runs


def _split_code_widget_score(run: list[LoginControl]) -> Optional[int]:
    """Score for a run of single-character boxes that is a one-time-code widget, else None. A box saying
    ``autocomplete=one-time-code`` or naming a code settles it; otherwise no box may label itself a
    date/phone/card/amount part, and the group must call itself a code (class names count: ancestors are
    where widgets say ``otp``) or be all digit-only boxes. Ancestor classes never veto: a generic wrapper
    such as Bootstrap's ``.card`` says nothing about the boxes inside it."""
    if len(run) not in _SPLIT_BOXES:
        return None
    if any("one-time-code" in c.autocomplete.lower().split() for c in run):
        return 100
    labels = [_words(c.name, c.label) for c in run]
    if any(_RE_OTP.search(label) for label in labels):
        return 95
    if _RE_NOT_A_CODE.search(" ".join(labels)):
        return None
    if _RE_CODE_WIDGET.search(" ".join(_words(c.name, c.label, c.hint) for c in run)):
        return 90
    return 80 if all(_digit_only(c) for c in run) else None


def classify_otp_controls(controls: list[LoginControl]) -> list[ClassifiedLoginControl]:
    """The controls that take a second-factor code. ``autocomplete=one-time-code`` is authoritative;
    otherwise a text/tel/number input whose name/label says code/OTP/2FA/verification. Some sites split
    the code into one input per digit (``maxlength=1`` boxes, often with no name or label at all): the
    whole group is returned in DOM order (see ``_split_code_widget_score``) and the fill spreads the code
    across it."""
    grouped: dict[int, int] = {}
    for run in _box_runs(controls):
        score = _split_code_widget_score(run)
        if score is not None:
            grouped.update((c.index, score) for c in run)
    out: list[ClassifiedLoginControl] = []
    for c in controls:
        if c.index in grouped:
            out.append(ClassifiedLoginControl(c, grouped[c.index], "one-time-code"))
            continue
        tokens = c.autocomplete.lower().split()
        if "one-time-code" in tokens:
            out.append(ClassifiedLoginControl(c, 100, "one-time-code"))
            continue
        if c.type not in _CODE_INPUT_TYPES:
            continue
        if _RE_OTP.search(_normalize_text(" ".join(p for p in (c.name, c.label) if p))):
            out.append(ClassifiedLoginControl(c, 70, "one-time-code"))
    return out


def select_password_fill(
    classified: list[ClassifiedLoginControl],
    password: str,
) -> list[dict[str, Any]]:
    """Select the single best current-password control to fill.

    The vault fill path is password-only: the identifier is agent-visible
    metadata and is typed by the agent via normal input tools. This picks
    the highest-scoring ``current-password`` control (ties broken by DOM
    order) and returns ``[{"index": int, "token": "current-password",
    "value": password}]`` or ``[]`` when no password field exists.
    """
    passwords = [c for c in classified if c.token == "current-password"]
    if not passwords or not password:
        return []
    best_password = min(passwords, key=lambda c: (-c.score, c.control.index))
    return [
        {
            "index": best_password.control.index,
            "token": "current-password",
            "value": password,
        }
    ]


def classify_checkout_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control as a payment/address fill target (autocomplete token exact match 100,
    label/name heuristic 70), or None. Password/email inputs are never checkout targets."""
    tokens = [t for t in control.autocomplete.lower().split() if t]
    for token in PAYMENT_AUTOFILL_TOKENS + ADDRESS_AUTOFILL_TOKENS:
        if token in tokens:
            return ClassifiedLoginControl(control, 100, "country-name" if token == "country" else token)
    if control.type in ("password", "email"):
        return None
    searchable = _normalize_text(" ".join(part for part in (control.name, control.label) if part))
    for pattern, token in _CHECKOUT_HEURISTICS:
        if pattern.search(searchable):
            return ClassifiedLoginControl(control, 70, token)
    return None


def select_checkout_fills(classified: list[ClassifiedLoginControl], secret: dict[str, str],
                          field_tokens: dict[str, str]) -> list[dict[str, Any]]:
    """Map a payment/address secret payload onto the best control per autocomplete token.

    ``field_tokens`` is ``PAYMENT_FIELDS`` / ``ADDRESS_FIELDS`` (agent/vault_store.py). A combined
    ``cc-exp`` control gets ``MM/YY`` from exp_month + exp_year and then suppresses the separate
    month/year fills. Returns ``[{"index", "token", "value"}]``: one control per token, highest
    score then DOM order.
    """
    values: dict[str, str] = {tok: secret[f] for f, tok in field_tokens.items() if secret.get(f)}
    if "cc-exp-month" in values and "cc-exp-year" in values:
        values["cc-exp"] = f"{values['cc-exp-month'].zfill(2)}/{values['cc-exp-year'][-2:]}"
    fills: list[dict[str, Any]] = []
    for token, value in values.items():
        candidates = sorted((c for c in classified if c.token == token), key=lambda c: (-c.score, c.control.index))
        if candidates:
            fills.append({"index": candidates[0].control.index, "token": token, "value": value})
    if any(f["token"] == "cc-exp" for f in fills):
        fills = [f for f in fills if f["token"] not in ("cc-exp-month", "cc-exp-year")]
    return fills


# JS expression evaluated in the page to inspect candidate input controls.
# Ported from OpenInstinct's nativeLoginControlInspectionExpression.
# Inspection stamps every input with ``<nonce>:<index>`` under a per-inspection attribute; the fill
# script resolves targets by the stamp of ITS OWN inspection instead of re-querying by position, so
# neither a DOM reflow nor a second inspection in between can redirect the password into another field.
INSPECTION_STAMP_ATTR = "data-hermes-vault-slot"


def build_otp_fills(otp_controls: list[ClassifiedLoginControl], code: str) -> list[dict[str, Any]]:
    """One fill per box. Default: the single best-scoring code field takes the whole code.

    Per-digit entry only when the page unmistakably uses it: exactly len(code) OTP controls that are all
    ``maxlength=1``, all in the same form, and adjacent in DOM order (the classic N-box widget). Anything
    looser (several code-like inputs scattered over a page) gets ONE field, never a digit sprayed across
    unrelated inputs, and never a single-character box: when only such boxes are left (a 6-digit code on a
    4-box page), nothing is filled and the caller says the code does not fit."""
    boxes = sorted((c for c in otp_controls if c.control.max_length == 1), key=lambda c: c.control.index)
    if (len(boxes) == len(code)
            and len({b.control.form_index for b in boxes}) == 1
            and all(b.control.index - a.control.index == 1 for a, b in itertools.pairwise(boxes))):
        return [{"index": b.control.index, "token": "one-time-code", "value": ch} for b, ch in zip(boxes, code)]
    fields = [c for c in otp_controls if c.control.max_length != 1]
    if not fields:
        return []
    best = max(fields, key=lambda c: c.score)
    return [{"index": best.control.index, "token": "one-time-code", "value": code}]


def build_inspection_js(nonce: str) -> str:
    return _LOGIN_CONTROL_INSPECTION_JS_TEMPLATE.replace("__NONCE__", json.dumps(nonce))


_LOGIN_CONTROL_INSPECTION_JS_TEMPLATE = """(() => {
  const nonce = __NONCE__;
  // Shadow-piercing collect: many logins (Home Assistant, custom-element SPAs) render their
  // form inside a shadow root, where document.querySelectorAll never sees it. Walk every
  // element (shadow hosts can be anything, not just inputs) and recurse into open shadow roots.
  // Forms are collected in the same walk: document.forms misses a <form> inside a shadow root, which
  // would leave its controls ungrouped (formIndex null) and merge split OTP boxes across forms.
  const elements = [];
  const forms = [];
  (function walk(root) {
    for (const e of root.querySelectorAll("*")) {
      if (e.tagName === "INPUT" || e.tagName === "SELECT") elements.push(e);
      else if (e.tagName === "FORM") forms.push(e);
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  })(document);
  elements.forEach((element, index) => element.setAttribute("data-hermes-vault-slot", nonce + ":" + index));
  const out = elements.flatMap((element, index) => {
    if (element.disabled || element.readOnly) return [];
    if (["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"].includes(element.type)) return [];
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || element.getClientRects().length === 0) return [];
    const labels = element.labels ? Array.from(element.labels, (l) => l.textContent || "") : [];
    const ariaText = (element.getAttribute("aria-labelledby") || "")
      .split(/\\s+/).filter(Boolean)
      .map((id) => { const r = element.getRootNode(); const n = (r.getElementById ? r.getElementById(id) : null) || document.getElementById(id); return n ? (n.textContent || "") : ""; })
      .join(" ");
    const resolvedFormIndex = element.form ? forms.indexOf(element.form) : -1;
    const hint = [];
    for (let n = element, depth = 0; n && depth < 3; n = n.parentElement, depth += 1) {
      hint.push(typeof n.className === "string" ? n.className : "", n.getAttribute("data-testid") || "");
      if (depth > 0) hint.push(n.id || "", n.getAttribute("aria-label") || "");
    }
    return [{
      autocomplete: element.autocomplete || "",
      formIndex: resolvedFormIndex >= 0 ? resolvedFormIndex : null,
      index,
      maxLength: element.maxLength > 0 ? element.maxLength : null,
      hint: hint.filter(Boolean).join(" ").slice(0, 400),
      pattern: element.getAttribute("pattern") || "",
      inputMode: element.getAttribute("inputmode") || "",
      label: [
        ...labels,
        element.getAttribute("aria-label") || "",
        ariaText,
        element.getAttribute("placeholder") || "",
        element.getAttribute("title") || "",
      ].join(" "),
      name: [element.name, element.id].join(" "),
      type: element.tagName === "SELECT" ? "select" : (element.type || ""),
    }];
  });
  return JSON.stringify(out);
})()"""


def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str = "") -> str:
    """Build a JS expression that fills the selected controls and reports only a count. The
    returned expression never echoes the values back.

    ``expected_origin`` is asserted against ``window.location.origin`` synchronously inside the SAME
    evaluated script, immediately before any write. If the page navigated between inspection and fill
    (TOCTOU), the script writes nothing and returns ``{"refused": "origin_changed", "found": <actual>}``:
    proof scope equals mutation scope (#88706). Targets resolve by the ``<nonce>:<index>`` stamp of
    THIS inspection; a ``current-password`` fill additionally requires ``type=password``; ``<select>``
    controls (country, state, expiry month) match an option by value or visible text. No marker is
    left on filled controls so later model-driven DOM reads cannot address them deterministically.
    """
    payload = json.dumps(
        [{"index": f["index"], "token": f.get("token", "current-password"), "value": f["value"]} for f in fills]
    )
    return (_FILL_JS_TEMPLATE.replace("__EXPECTED_ORIGIN__", json.dumps(expected_origin))
            .replace("__FILLS__", payload).replace("__NONCE__", json.dumps(nonce)))


_FILL_JS_TEMPLATE = """(() => {
  const expectedOrigin = __EXPECTED_ORIGIN__;
  if (window.location.origin !== expectedOrigin) {
    return JSON.stringify({ refused: "origin_changed", found: window.location.origin });
  }
  const fills = __FILLS__;
  const nonce = __NONCE__;
  // Stamped controls may live inside a shadow root (see the inspection script's walk): collect the
  // document and every open shadow root once, then look each stamp up in those roots.
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) if (e.shadowRoot) walk(e.shadowRoot);
  })(document);
  const find = (selector) => roots.flatMap((r) => Array.from(r.querySelectorAll(selector)));
  let filled = 0;
  const norm = (t) => String(t || "").trim().toLowerCase();
  for (const f of fills) {
    const el = find('[data-hermes-vault-slot="' + nonce + ':' + f.index + '"]')[0];
    if (!el || (f.token === "current-password" && el.type !== "password")) continue;
    try {
      if (el.tagName === "SELECT") {
        const want = norm(f.value);
        const opt = Array.from(el.options).find((o) => [o.value, o.textContent].some((t) => norm(t) === want || norm(t) === want.replace(/^0/, "")));
        if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true, composed: true })); filled += 1; }
        continue;
      }
      el.focus();
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      // One box of a split code widget (f.value is THIS box's character, see build_otp_fills). Widgets built
      // on keydown/keyup (Vue/React OTP components advance focus and commit the digit there) need the key
      // events a typing user would send around the value change.
      const key = f.token === "one-time-code" && f.value.length === 1
        ? { key: f.value, code: /^[0-9]$/.test(f.value) ? "Digit" + f.value : "", bubbles: true, composed: true, cancelable: true }
        : null;
      if (key) el.dispatchEvent(new KeyboardEvent("keydown", key));
      if (setter && setter.set) { setter.set.call(el, f.value); } else { el.value = f.value; }
      // composed: a component listening on its shadow host must see the value change too.
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: key ? f.value : null }));
      if (key) el.dispatchEvent(new KeyboardEvent("keyup", key));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (el.value.length > 0) filled += 1;
    } catch (e) { /* skip */ }
  }
  find("[data-hermes-vault-slot]").forEach((n) => n.removeAttribute("data-hermes-vault-slot"));
  return JSON.stringify({ filled });
})()"""
