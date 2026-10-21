# Hermex 3.2.1 all-time usage API

Source-only draft contract. No deployment or activation is part of this change. The authenticated dashboard WebSocket uses the existing JSON-RPC 2.0 transport and `usage.rollup` method.

## Request

```json
{"jsonrpc":"2.0","id":1,"method":"usage.rollup","params":{"profile":"default","window":"all","timezone":"UTC","limit":20}}
```

| Parameter | Type | Default and meaning |
|---|---|---|
| `profile` | string or null, optional | Launch profile when omitted or null. A nonblank named profile selects that profile alone. Apps send their selected profile. |
| `window` | `"today"`, `"7d"`, `"30d"`, or `"all"`, optional | `"7d"`. Only `"all"` selects retained session counters. |
| `timezone` | IANA timezone string, optional | `"UTC"`. Validated and echoed for all-time. It does not partition or filter lifetime counters. |
| `session_id` | stored session ID string or null, optional | No filter. A present ID selects its owning accounting chat. An unknown ID produces an empty result. This is not a live runtime ID. |
| `limit` | strict integer, optional | 20. Range 1 through 100. Limits only `top_chats`, never totals, model rows, provider rows, or coverage. Booleans are invalid. |

Unknown parameter keys are invalid. An explicitly blank profile is invalid. The existing windows keep their existing response payloads and timestamped-ledger semantics.

## All-time response types

All listed fields are present. JSON `number` fields can be integer-valued. Epoch timestamps use seconds, not milliseconds. JSON object property order is not significant.

```text
AllTimeUsageRollupResult = {
  profile: string,
  window: "all",
  source: "session_totals",
  start: number | null,
  end: number,
  timezone: string,
  totals: AllTimeMetrics,
  agents: AllTimeAgentUsage[],
  models: AllTimeModelUsage[],
  providers: AllTimeProviderUsage[],
  days: [],
  top_chats: AllTimeChatUsage[],
  chat_count: integer,
  coverage: ProviderCoverage[],
  coverage_since: number | null,
  partial: true,
  coverage_reason: string,
  basis: "standard_api_list_price",
  currency: "USD"
}

AllTimeMetrics = {
  input_tokens: integer,
  output_tokens: integer,
  cache_read_tokens: integer,
  cache_write_tokens: integer,
  reasoning_tokens: integer,
  api_call_count: integer,
  total_tokens: integer,
  cache_hit_percent: number | null,
  estimated_cost_usd: number | null,
  known_estimated_cost_usd: number,
  unknown_price_events: integer,
  cost_status: "est" | "unknown",
  cost_reason: string | null,
  pricing_dates: string[],
  price_sources: string[],
  turn_count: null,
  event_count: null,
  unattributed_turn_events: null
}

AllTimeAgentUsage = { profile: string, ...AllTimeMetrics }
AllTimeModelUsage = { model: string, provider: string, ...AllTimeMetrics }
AllTimeProviderUsage = { provider: string, ...AllTimeMetrics }
AllTimeChatUsage = { session_id: string, title: string | null, ...AllTimeMetrics }
ProviderCoverage = {
  provider: string,
  since: number,
  chat_count: integer,
  session_count: integer
}
```

`...AllTimeMetrics` means every metrics field is flattened onto that row, not nested. The generated Python, TypeScript, and OpenRPC contracts distinguish this result from the unchanged timestamped result. In TypeScript the method result is `UsageRollupWireResult`, a union of `UsageRollupResult` and `AllTimeUsageRollupResult`.

## Counter and attribution semantics

`source:"session_totals"` means every retained `sessions` row contributes its stored running counters exactly once. Rows before and after the timestamped collector's installation are included. The method never adds `usage_events` or `session_model_usage` to these counters. Auxiliary-only counters that exist only in those other tables are outside this all-time view.

Compression continuations and delegated work belong to their owning chat. An explicit branch keeps its own spend. `session_id` filters use the same ownership rules. A cyclic parent chain is bounded and never drops its retained counters. Missing parents leave the retained child independent.

Each session's entire counters use that session's stored `model` and `billing_provider`. This is stored metadata attribution, not reconstructed model-switch history. A session that changed providers or models can contain mixed historical usage. Missing metadata becomes an empty string, not a guessed provider or model.

Token buckets are disjoint, except `reasoning_tokens` is already included in output. `total_tokens` is input plus output plus cache-read plus cache-write. Cache-hit percent is cache-read divided by input plus cache-read plus cache-write, multiplied by 100. It is null when there are no prompt tokens.

`api_call_count` sums retained call counters. `turn_count`, `event_count`, and `unattributed_turn_events` are null because sessions do not retain the required execution identities or request events. Null does not mean zero work. No message count substitutes for a turn count.

`agents` has one row, with this profile and the same metrics as `totals`. Other profiles require separate calls. App-side combined results preserve `(connection, profile, session_id)` identity and visibly incomplete profiles. This method does not discover or merge profiles.

`models` groups by stored model and provider. `providers` groups by stored provider. Both sort by tokens descending, then their identifiers. `top_chats` sorts by total tokens descending, then stored owning-chat ID. Unknown price does not move a chat to the bottom. `chat_count` counts every matching distinct owning chat, including zero-counter chats and chats beyond `limit`.

`title` uses the same stored owning-chat title as the existing rollup, or null when absent. The method does not derive titles from message content and adds no title exposure outside the existing authenticated, profile-scoped route.

## Coverage and dates

`coverage` has one row for every included stored provider, including `""` for missing provider metadata. Rows sort by provider identifier.

- `since` is that provider's earliest included session `started_at`.
- `session_count` is the number of retained session rows in that provider bucket.
- `chat_count` is the number of distinct owning chats in that provider bucket.

A chat spanning providers can appear in several coverage rows. Provider chat counts are not additive. Session counts and token totals are additive. Coverage follows the chat filter, not the top-chat limit.

`coverage_since` and `start` are the earliest included session start across providers. Both are null when no session matches. `end` is the server observation time. It does not turn cumulative counters into request timestamps or impose a historic request window.

`partial` is always true for all-time. These are retained, reported counters, not a claim of complete provider history. A coverage date proves only the oldest included chat start. It does not prove every call since that date was measured or every prior chat was retained. `coverage_reason` states these limitations.

`days` is always an empty array. All-time has no daily series. Neither session starts nor end times can distribute cumulative usage across days. The app must not fill a daily chart from this result.

## Price and null rules

Every dollar amount is an EST standard API list-price equivalent, never billed spend, subscription cost, or an invoice reconstruction. Stored billed cost is not used. Pricing uses the same offline `estimate_api_value` estimator, exact model IDs, provider handling, published cache-bucket support, and snapshot as `usage.estimate`. Codex subscription inclusion does not make its API value free.

A row's `estimated_cost_usd` is null when any included session cannot be priced. `known_estimated_cost_usd` is the known subtotal only. `unknown_price_events` counts unpriced **session rows** for this source, not actual request events. The legacy name is retained to keep the metrics envelope familiar. `cost_status` is `"unknown"` if that count is nonzero, otherwise `"est"`. `cost_reason` is nonempty for unknown buckets and null for fully priced buckets.

Unknown model, unknown provider, invalid token counters, or an unpriced nonzero token bucket stays null, never zero. A completed-call counter without any measured tokens also has unknown price. A zero-token chat with unknown model or provider still has unknown price. An empty selection has zero tokens and zero dollars, no coverage, and empty row arrays other than the single zero-total agent row.

Request context tiers require special treatment. When a session does not establish exactly one API call, the reader compares the estimator's request-tier value with its linear unit rates. If they differ, price is null with reason `per-chat totals: original call boundaries unavailable for context-tier pricing`. It does not apply a long-context multiplier to an entire multi-call chat, split a chat into invented requests, or silently assume the cheaper tier. Counts remain included. A one-call session can use its request tier. Linear-price models and safely below-threshold cumulative counters use the estimator directly. The reader does not duplicate model-specific thresholds or rate tables.

`pricing_dates` contains sorted unique snapshot dates as `YYYY-MM-DD` strings. `price_sources` contains sorted unique source URLs where available. They describe the current estimate, not the original request's invoice or an original historical price. An unknown provider can have no source URL.

## Support detection and errors

An app can probe `usage.rollup` with valid parameters and `window:"all"` for its selected profile. Success requires `window:"all"` and `source:"session_totals"`. The old server already has the method but rejects the new value with existing code 4002. That response maps to "Not available yet" for this known-valid support probe. It does not authorize a fallback that relabels a 30-day total as all-time.

| Error code | Meaning |
|---|---|
| `-32601` | The server lacks `usage.rollup`. All-time is unavailable. |
| `4000` | Unknown parameter key. |
| `4002` | Invalid parameter, unsupported window, invalid timezone, invalid limit, or blank explicit profile. Outside a known-valid support probe, this is a request error. |
| `4064` | Requested profile does not exist. No default-profile fallback. |
| `5036` | Selected usage storage is unavailable or lacks required session columns. No database creation, migration, schema upgrade, or transcript fallback. The existing error wording is retained. |
| `5035` | Backend retirement admission refused the request. Reconnect. |

Error envelope:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":4002,"message":"invalid or missing method parameters"}}
```

The route keeps existing transport admission and profile resolution. It opens the selected `state.db` with `mode=ro`, enables `query_only`, and uses one read transaction. SQL performs ownership, sums, grouping, coverage, and top-chat selection. The Python pricing function operates on session counters, never on messages. No state table, schema migration, index, live writer flush, or new network lookup is required.

## Full fictional example

Every identifier, title, counter, and observation time in this example is fictional. The example comes from a disposable SQLite fixture through the public RPC and passes the complete result contract.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "profile": "default",
    "window": "all",
    "source": "session_totals",
    "start": 1784894400.0,
    "end": 1792500000.0,
    "timezone": "UTC",
    "totals": {
      "input_tokens": 1000,
      "output_tokens": 200,
      "cache_read_tokens": 500,
      "cache_write_tokens": 100,
      "reasoning_tokens": 50,
      "api_call_count": 2,
      "turn_count": null,
      "event_count": null,
      "unattributed_turn_events": null,
      "known_estimated_cost_usd": 0.0086,
      "unknown_price_events": 0,
      "cost_reason": null,
      "pricing_dates": ["2026-10-19"],
      "price_sources": ["https://platform.claude.com/docs/en/about-claude/pricing"],
      "total_tokens": 1800,
      "cache_hit_percent": 31.25,
      "estimated_cost_usd": 0.0086,
      "cost_status": "est"
    },
    "agents": [{
      "profile": "default",
      "input_tokens": 1000,
      "output_tokens": 200,
      "cache_read_tokens": 500,
      "cache_write_tokens": 100,
      "reasoning_tokens": 50,
      "api_call_count": 2,
      "turn_count": null,
      "event_count": null,
      "unattributed_turn_events": null,
      "known_estimated_cost_usd": 0.0086,
      "unknown_price_events": 0,
      "cost_reason": null,
      "pricing_dates": ["2026-10-19"],
      "price_sources": ["https://platform.claude.com/docs/en/about-claude/pricing"],
      "total_tokens": 1800,
      "cache_hit_percent": 31.25,
      "estimated_cost_usd": 0.0086,
      "cost_status": "est"
    }],
    "models": [{
      "model": "claude-opus-5-5",
      "provider": "anthropic",
      "total_tokens": 1800,
      "input_tokens": 1000,
      "output_tokens": 200,
      "cache_read_tokens": 500,
      "cache_write_tokens": 100,
      "reasoning_tokens": 50,
      "api_call_count": 2,
      "turn_count": null,
      "event_count": null,
      "unattributed_turn_events": null,
      "known_estimated_cost_usd": 0.0086,
      "unknown_price_events": 0,
      "cost_reason": null,
      "pricing_dates": ["2026-10-19"],
      "price_sources": ["https://platform.claude.com/docs/en/about-claude/pricing"],
      "cache_hit_percent": 31.25,
      "estimated_cost_usd": 0.0086,
      "cost_status": "est"
    }],
    "providers": [{
      "provider": "anthropic",
      "total_tokens": 1800,
      "input_tokens": 1000,
      "output_tokens": 200,
      "cache_read_tokens": 500,
      "cache_write_tokens": 100,
      "reasoning_tokens": 50,
      "api_call_count": 2,
      "turn_count": null,
      "event_count": null,
      "unattributed_turn_events": null,
      "known_estimated_cost_usd": 0.0086,
      "unknown_price_events": 0,
      "cost_reason": null,
      "pricing_dates": ["2026-10-19"],
      "price_sources": ["https://platform.claude.com/docs/en/about-claude/pricing"],
      "cache_hit_percent": 31.25,
      "estimated_cost_usd": 0.0086,
      "cost_status": "est"
    }],
    "days": [],
    "top_chats": [{
      "session_id": "fictional-chat",
      "title": "Fictional example",
      "total_tokens": 1800,
      "input_tokens": 1000,
      "output_tokens": 200,
      "cache_read_tokens": 500,
      "cache_write_tokens": 100,
      "reasoning_tokens": 50,
      "api_call_count": 2,
      "turn_count": null,
      "event_count": null,
      "unattributed_turn_events": null,
      "known_estimated_cost_usd": 0.0086,
      "unknown_price_events": 0,
      "cost_reason": null,
      "pricing_dates": ["2026-10-19"],
      "price_sources": ["https://platform.claude.com/docs/en/about-claude/pricing"],
      "cache_hit_percent": 31.25,
      "estimated_cost_usd": 0.0086,
      "cost_status": "est"
    }],
    "chat_count": 1,
    "coverage": [{"provider": "anthropic", "since": 1784894400.0, "chat_count": 1, "session_count": 1}],
    "coverage_since": 1784894400.0,
    "partial": true,
    "coverage_reason": "Retained per-chat running totals, not complete provider history. Coverage dates are earliest included chat starts, not request timestamps. Stored model/provider attribution may span model switches; auxiliary-only counters are not included.",
    "basis": "standard_api_list_price",
    "currency": "USD"
  }
}
```

## Source and regeneration

The Python declaration is `tui_gateway/contracts/usage.py`. The all-time SQL reader is `hermes_state_alltime.py`. The existing RPC adapter is `tui_gateway/methods_usage.py`.

Generated files are never hand-edited. Their generator is `python scripts/gen_gateway_contracts.py`. The committed files are `apps/shared/src/gateway-contract.generated.ts` and `apps/shared/src/gateway-contract.openrpc.json`.

Server activation remains a separate owner-approved immutable release. The app can ship first with the support probe. The all-time display remains unavailable until the paired server change is live.
