# Content-free tier evidence

The `agent.tier_evidence` INFO logger records requested and provider-reported tiers
separately at the existing Responses and Anthropic SDK boundaries. It does not
change routing, speed selection, pricing, fallback behavior or configuration.
There is no new output file, plugin, outbound telemetry or per-token log emission.

## Where the observations happen

- Responses: the final `responses.create(stream=True)` kwargs, after Relay's
  request callback, consumer sanitization and SDK-transform bypass. Raw
  `response.completed`, `response.incomplete` and `response.failed` events are
  observed **before** Relay can rewrite them.
- Anthropic main-loop streaming: the final sanitized `messages.stream` kwargs,
  followed by raw SDK events. The nominal non-streaming/auxiliary adapter's
  stream and `messages.create` fallback are covered too. This includes compatible
  endpoints using that adapter; `wire` identifies the protocol, not the account
  or provider identity.

Logging at request assembly or at usage accounting is insufficient: execution
middleware/Relay can rewrite requests later, and Relay can also rewrite responses.
The stream wrapper yields the original events, delegates SDK attributes and
cleanup, and preserves the existing null-usage normalization.

## Reading records

Read the existing profile-scoped agent log using the normal authorized log
interface. Select only records from logger `agent.tier_evidence`; the surrounding
log is **not** content-free. Join request/response records by `attempt`, a new
32-character random hex identifier generated locally for each SDK invocation.
The existing logging context can locate the session. No session ID, task ID,
account identifier, model name, provider response ID or headers are copied into
the collector's message.

The following is a **synthetic format example**, not a rollout receipt:

```text
tier_evidence phase=request attempt=0123456789abcdef0123456789abcdef wire=responses req_service_tier=priority req_speed=absent
tier_evidence phase=response attempt=0123456789abcdef0123456789abcdef wire=responses outcome=terminal served_tier=default served_source=response response_status=completed
```

This example requested priority but reports default. It is not served-priority
proof. Never substitute configuration or requested values for the response field.

Fields:

- `req_service_tier`, `req_speed`: final SDK kwargs, with `extra_body` precedence,
  including explicit null overrides. They describe the request, not its result.
- `served_tier`: only explicit response `service_tier`, or nested
  `usage.service_tier`. Anthropic stream usage may report it on `message_start`
  or `message_delta`; the latest non-null explicit report is retained. An invalid
  top-level report is `unknown`, not a reason to use a different nested value.
- `served_source`: `response`, `usage`, or `absent`.
- `response_status`: allowlisted explicit status; Responses terminal event type
  also supplies its explicit status. Anthropic status can remain `absent` even
  with a `message_stop`.
- `outcome`: `terminal` (a terminal event was received), `returned` (non-streaming
  SDK response), `error`, `closed` (early close), or `exhausted` (EOF without a
  terminal event). A terminal **failed/incomplete** response is not a successful
  completion. A reported tier on an interrupted stream remains a report, not
  proof of successful completion.

Missing/null metadata is `absent`. Unsupported, malformed, oversized or hostile
metadata is `unknown`; neither means normal, standard, priority or fast. Values
are not coerced, trimmed, serialized, or copied wholesale into the log. Only
bounded enums and the locally generated correlation ID are emitted.

## Acceptance and limitations

A served-tier acceptance requires an explicit provider report for the authorized
session/route, paired with its request and an appropriate terminal/completion
outcome. For example, absent Anthropic speed establishes only that the request
did not explicitly ask for fast; **absent served tier cannot establish normal**.
A wire label is not provider/account identity: establish routing separately.

Each SDK invocation emits a request and at most one response record. An abrupt
process exit can leave an unmatched request. SDK-internal HTTP retries are not
separate observations; explicit adapter retries receive fresh IDs. Other
transports (Chat Completions, app-server, Bedrock Converse, etc.) are not covered.
No live rollout is implied by synthetic tests or by source installation.

Do not enable request dumps or export whole logs to obtain this evidence. Preserve
normal profile/session access controls and retain only the selected metadata
needed for the authorized verification.

## Synthetic tests

Run through the repository's isolated runner, not bare pytest:

```bash
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/agent/test_tier_evidence.py \
  tests/agent/test_tier_evidence_filtering.py
```

The integration tests use real adapters with fake SDK I/O, rewrite requests and
responses at the Relay boundary, and assert the raw SDK tier survives separately.
They do not call live providers or contain private prompts or credentials.
