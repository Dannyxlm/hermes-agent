# Native usage and profile defaults

The dashboard's authenticated `/api/ws` JSON-RPC surface exposes four additive methods.
Older servers answer `-32601`; clients must gate these controls by method availability.
Existing `session.usage` billed-cost semantics and session-local `config.set` are unchanged.

## API value, not invoices

```json
{"jsonrpc":"2.0","id":1,"method":"usage.estimate","params":{"provider":"openai-codex","model":"gpt-6.1-sol","input_tokens":100000,"output_tokens":10000,"cache_read_tokens":50000}}
```

Result: `estimated_cost_usd:0.305`, `status:"est"`, `reason:null`,
`pricing_date:"2026-10-19"`, `source_url:"https://developers.openai.com/api/docs/pricing"`,
`basis:"standard_api_list_price"`, `currency:"USD"`.

Count parameters are optional nonnegative integers (default zero). `input_tokens` is
uncached input; cache read/write are disjoint. Output already includes reasoning.

The offline table in `agent/api_value_pricing.py` records published standard API rates,
including OpenAI/Codex and Claude, independently of subscription billing. Exact model ids
only; unknown model/provider/rate returns null cost and a reason. No request-time network
pricing lookup. The existing server-verified Codex context-variant canonicalizer is reused.

Sources, retrieved 2026-10-19:
- https://developers.openai.com/api/docs/pricing
- https://developers.openai.com/api/docs/models/gpt-6.1-sol
- https://developers.openai.com/api/docs/models/gpt-5.3-codex
- https://platform.claude.com/docs/en/about-claude/pricing

This is **EST / standard API value**, not billed spend or remaining plan allowance.
Discounts, speed tiers, regional processing, tools and tax are excluded. Claude cache writes
use published 5-minute rates. Published OpenAI long-context multipliers apply to the whole
request above 272K input/cache tokens, not to a sum across requests.

## Windowed usage

`usage.rollup` params: optional `profile`, `window` (`today`, `7d`, `30d`; default `7d`),
`timezone` (IANA, default `UTC`), stored `session_id` filter, `limit` (1–100, default 20).
Calendar windows include today and end now in the requested timezone, including DST.

Result contains `profile`, epoch `start`/`end`, `timezone`, `totals`, `agents`, `models`,
`providers`, `days`, `top_chats`, `chat_count`, `coverage_since`, `partial`, `coverage_reason`,
`basis` and `currency`. Generated contracts declare every nested field.

Metrics include input/output/cache-read/cache-write/reasoning tokens, total tokens, API-call
count, distinct execution-turn count, event count, unattributed main-call events, cache-hit
percentage, estimated cost, known cost subtotal, unknown-price event count/reason, status,
pricing dates and source URLs. Reasoning is a subset of output, not an extra token bucket.
Cache-hit percentage uses cache-read / total prompt tokens; null for no prompt tokens.

One profile per request preserves profile isolation. An all-agents client calls once per known
profile with the same window/timezone, keeps errors visible and never merges equal stored
IDs across profiles. `agents` contains that profile's aggregate. Top chats rank by token
usage descending (stored ID breaks ties); `limit` never limits totals or `chat_count`.
Compression and delegated work belong to their owner chat; explicit branches remain separate.
Model rows include provider. Turn counts spanning models/days are not additive. Auxiliary
calls contribute tokens/cost without inventing execution turns.

Unknown cost in any bucket makes its full estimated cost null; `known_estimated_cost_usd`
is only the known subtotal. Completed calls without measured tokens remain unknown, not free.

**Historical coverage:** existing lifetime counters have only first/last timestamps and cannot
be split honestly into days. The additive `usage_events` collector is prospective. Responses
explicitly mark windows predating installation as partial. Do not infer historical days from
cumulative counters or render uncovered days as measured zero. Incremental accounting records
events inside its existing transaction; cumulative absolute checkpoints do not double charge.
Coalesced writes preserve each call's original timestamp, turn and pricing context.

Requests open SQLite `mode=ro`, `query_only`, with timestamp/chat-time indexes. They never
create storage, migrate, flush writers or scan transcripts. Results may lag the async writer.
`5036` means storage/schema unavailable; no transcript fallback.

## New-chat defaults

`config.defaults.get {profile}` returns main model (read-only here), reasoning level and
server reasoning vocabulary, delegation model/provider, `applies_to:"new_chats_and_delegations"`
and precedence: session > per-model > profile default.

```json
{"jsonrpc":"2.0","id":2,"method":"config.defaults.set","params":{"profile":"default","key":"reasoning","value":"low","confirm":true}}
{"jsonrpc":"2.0","id":3,"method":"config.defaults.set","params":{"profile":"default","key":"delegation","value":"gpt-6.1-sol","provider":"openai-codex","confirm":true}}
```

Explicit nonblank profile, authenticated non-internal human transport and confirmation are
required. Reasoning accepts `none` plus the server vocabulary; display words are not levels.
Use model capabilities to display the model-appropriate subset. Delegation requires a
recognized, accepted, persistable model from the existing provider/catalog validator.
Empty delegation model+provider restore inheritance. A configured direct delegation endpoint
or key is refused by this short control: changing its route belongs in full settings.

Result is the GET shape plus `changed` and opaque `operation_id`. Equal values do not rewrite
config or duplicate audit records (`changed:false`, `operation_id:null`). New values use the
existing dashboard config lock and canonical atomic YAML writer, with durable intent/applied
audit records and disk readback. Clients should GET the same profile after success. After a
write/audit error, read back before deciding whether to retry; never blindly replay a write.

Reasoning affects newly constructed chats; existing live settings are untouched. Delegation
changes apply to future child agents, including future handoffs in already-open chats, not to
children already running. Main default model stays on `/api/model/set` (`scope:"main"`).

Errors: `4000` unknown param key; `4002` invalid/missing input, missing confirmation or
unsupported selection; `4064` unknown profile; `4403` unauthorized defaults writer; `5001`
default read/write/audit error; `5035` backend retiring; `5036` usage storage unavailable.

## Accounts (existing capability)

`model.options.providers[].usage` already exposes per-account quota windows when available.
Use fresh known `used_percent` and provider reset timestamps, not token spend, to show
remaining quota. Multi-account providers expose safe account IDs/labels and state; single
accounts retain legacy `usage.windows`. Model-scoped windows are not whole-account limits.
Fetched-at age, account plan/primary/order and per-account API-value attribution are not
provided by these new methods. Unknown/stale/unsupported quota remains unknown.
