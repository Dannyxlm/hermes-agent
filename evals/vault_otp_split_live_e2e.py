"""Live E2E: enter_code on a split six-box code widget, in the tab browser_exec is driving.

Reproduces a white-labeled GoHighLevel email-verification page in a real Chromium through
the REAL browser_exec path: six unnamed ``<input class="ui-otp-field-input" type="number" maxlength="1"
pattern="[0-9]" placeholder="0">`` boxes driven by a Vue-style component that keeps the digits in its own model
and re-renders the boxes from it (a value that never reached the model is wiped). Around it:

- a second tab on the same origin with the same widget (a URL-equal neighbour the vault must not touch);
- a tab on another origin that the supervisor is attached to (the unrelated pinned tab of the incident);
- the agent moves to the code tab with ``switch_tab`` (no activation), exactly as in the incident.

``browser_vault enter_code`` with the saved login's handle must fill the driven tab only, one digit per box,
and the component's model must hold the code. The code is a fixture; it comes from a stand-in for the
login's inbox source and never appears in the tool result.

Run: HERMES_E2E_BROWSER=1 <venv>/bin/python evals/vault_otp_split_live_e2e.py
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="hermes-vault-otp-e2e-"))
os.environ["HERMES_HOME"] = str(HOME)

FIXTURE_CODE = "246810"
TASK = "vault-otp-e2e"

_VERIFY = b"""<!doctype html><title>Verify your email</title>
<div class="crm-card card"><h2>Enter the 6-digit code we emailed you</h2>
<div class="ui-otp-field" id="otp"></div><button id="verify" disabled>Verify</button></div>
<script>
const state = Array(6).fill("");
const root = document.getElementById("otp");
const boxes = state.map((_, i) => {
  const el = document.createElement("input");
  el.className = "ui-otp-field-input"; el.type = "number"; el.min = "0"; el.max = "9"; el.placeholder = "0";
  el.setAttribute("maxlength", "1"); el.setAttribute("pattern", "[0-9]");
  el.addEventListener("input", () => { const v = String(el.value).slice(-1); state[i] = /[0-9]/.test(v) ? v : ""; render(); });
  el.addEventListener("keyup", (e) => { if (/^[0-9]$/.test(e.key) && i < 5) boxes[i + 1].focus(); });
  root.appendChild(el);
  return el;
});
function render() {
  boxes.forEach((b, j) => { if (b.value !== state[j]) b.value = state[j]; });
  document.getElementById("verify").disabled = state.join("").length !== 6;
  window.__committed = state.join("");
}
render();
</script>"""

PAGES = {
    "/verify": _VERIFY,
    "/verify-other": _VERIFY,
    "/dashboard": b"<!doctype html><title>Dashboard</title><input name=q placeholder=Search>",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?", 1)[0], b"nope")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _exec(code: str) -> dict:
    from tools import browser_use_cli as bu

    out = json.loads(bu.browser_exec(code, task_id=TASK, timeout_s=90))
    assert out.get("success"), out
    return out


def _committed(sup, origin: str, path: str) -> str:
    """The component model of the tab at ``path`` (raw supervisor read, never a model surface)."""
    assert sup.focus_page(origin, prefer_url=origin + path)["ok"]
    res = sup.evaluate_runtime("location.pathname + '|' + window.__committed + '|' + document.getElementById('verify').disabled")
    return str(res.get("result"))


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    site = f"http://127.0.0.1:{port}"  # the CRM stand-in
    elsewhere = f"http://localhost:{port}"  # another origin: the unrelated pinned tab

    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    try:
        _exec("new_tab('about:blank')")
        out = _exec(f"new_tab({site + '/verify'!r}); wait_for_load(); print(current_tab()['targetId'])")
        driven = out["output"].strip().splitlines()[-1]
        _exec(f"new_tab({site + '/verify-other'!r}); wait_for_load()")
        _exec(f"new_tab({elsewhere + '/dashboard'!r}); wait_for_load()")

        sup = SUPERVISOR_REGISTRY.get(TASK)
        assert sup is not None, "browser_exec did not attach a supervisor for its task"
        assert sup.focus_page(elsewhere)["ok"]  # the supervisor sits on the other site's tab, as in the incident
        print("supervisor page before enter_code:", sup.evaluate_runtime("location.href")["result"])

        _exec(f"switch_tab({driven!r}); print(page_info()['url'])")
        print("driven tab recorded by browser_exec:", SUPERVISOR_REGISTRY.driven_tab(TASK))
        assert SUPERVISOR_REGISTRY.driven_tab(TASK) == (site + "/verify", driven)

        from agent import redact
        from agent.vault_store import get_vault_store
        from tools import browser_vault_tool as bvt

        login = get_vault_store().add_item("login", "crm-login", {"identifier_type": "email", "identifier": "a@b.c",
                                                                "password": "pw-E2E-OTP-3141"}, origin=site)
        bvt._email_code = lambda backend, handle, host: FIXTURE_CODE  # stand-in for the login's inbox source
        raw = bvt.browser_vault_enter_code(login.id, task_id=TASK)
        out = json.loads(raw)
        print("enter_code:", out)
        assert out["success"] and out["filled_fields"] == 6 and out["origin"] == site, out
        assert FIXTURE_CODE not in raw

        driven_state = _committed(sup, site, "/verify")
        other_state = _committed(sup, site, "/verify-other")
        print("driven tab model:", driven_state.replace(FIXTURE_CODE, "<fixture code>"), "| neighbour:", other_state)
        assert driven_state == f"/verify|{FIXTURE_CODE}|false", driven_state  # model holds it, Verify enabled
        assert other_state == "/verify-other||true", other_state
        redact.clear_vault_redaction_values()
        print("E2E OK")
        return 0
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        from tools.browser_session_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
        srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
