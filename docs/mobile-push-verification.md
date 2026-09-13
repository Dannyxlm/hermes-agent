# Mobile push backend unit verification — 2026-09-12

Scope: Hermes source baseline `b66b106bb32050495523424f1155ad0e3ef72edc`.
This receipt covers local implementation tests, not deployment or Apple delivery.

The first new durable-restart test was run through the canonical runner before
`tui_gateway.mobile_push` existed and failed collection with ModuleNotFoundError.
After implementation it exercised real temporary SQLite, persisted run identity,
completion deduplication, principal-scoped removal, and exact native destinations.

Final focused run:

```sh
HERMES_PYTHON=/Users/dannym/Developer/test-envs/project-link-20260908/bin/python \
  scripts/run_tests.sh tests/tui_gateway/test_mobile_push.py \
  tests/tui_gateway/test_methods_mobile_push.py \
  tests/tui_gateway/test_bot_live_owner_delivery.py --file-timeout 120
```

Result: 19 tests passed (12 shared provider/store, four real native RPC/projection,
three existing mailbox/turn ownership). No retry-only passes.

Earlier adjacent focused runs passed 33 existing mobile contract tests and 39 existing
failed-turn, turn-failure-cause and compute-host protocol tests. The parent owns the
authoritative wider verification and release decision.

Behavior covered:

* Real scoped native dispatch rejects missing identity, wrong profile and detached
  registration; owned unregister works after the runtime disappears.
* Canonical completion persists before a detached transport returns false; a repeated
  terminal frame yields one generic alert without its transcript text.
* SQLite restart retains run identity and event digest; leases recover after expiry;
  retries are bounded; old-token 410 cannot delete a rotated registration and its
  pending completion can retry with the new token.
* Activity registration does not depend on alert permission. Cross-scope runs fail.
  Progress coalesces; terminal updates end the activity; rotation resends the end.
* An eight-hour activity ends without falsely ending long-running canonical work.
  The eventual real completion still generates its alert.
* Category opt-out gates queued alerts, and logout clears pending jobs even with
  the APNs provider disabled.
* A real ES256 test key and HTTPX mock transport verify fixed APNs topic, environment,
  payload and response handling. No credential or real device token was used.
* Corrupt optional storage closes the partially initialized sender and reports
  unavailable; canonical transport and snapshots still function.
* Five hundred progress transitions do not push ActivityKit timestamps into the
  future. Published transitions are ordered by real dispatch seconds.

Ruff on new modules/tests and `git diff --check` pass. A neighboring rebound-turn
fixture now supplies the new optional finalization collaborator; its mailbox/turn
ownership assertions remain intact.

Pending parent work: WebUI event adapters, app integration, CE review, immutable
backend activation, APNs credentials/capabilities, TestFlight builds, and physical
notification-tap/background-activity verification. None is claimed by this receipt.

## Reply previews — 2026-09-13

Authenticated per-device preview preferences now project a canonical Bot/Chats title
and completed assistant excerpt into the existing alert. Generic defaults, pending
alert opt-out, Unicode bounds, additive SQLite migration, detached native delivery,
and unchanged canonical destinations passed 72 focused and adjacent tests through
`scripts/run_tests.sh`. No live APNs send is implied by this test receipt.
