# Native ordinary-session delivery and reconnect cuts

This is an additive private-mobile protocol. It does not widen `mobile.open`,
`mobile.snapshot`, `mobile.submit`, or canonical `mobile.push.*` / `mobile.activity.*`
authority. These existing methods still require the exact canonical Bot Chat
root/profile/runtime and attached connection. Ordinary sessions use a separate
`native_session` destination. `mobile.capabilities` advertises all new methods and
`ordinary_session_push`, `ordinary_session_live_activity`,
`native_widget_snapshot_capability`, `atomic_stream_snapshot`, and
`rich_stream_snapshot` (version 1). `feature_versions.inbox_summaries = 1`
adds the read-only Inbox projections below.

## Inbox summaries v1

`GET /api/sessions?profile=<existing-profile>` retains its old fields and adds:

- Row `attention?: {kind: "approval" | "clarify" | "input", count: positive integer,
  revision: string}` from the loaded native supervisor's open request registry,
  bound to the exact stored ID and profile home. No runtime is resumed/attached.
- Row `last_assistant_reply?: {row_id: integer, at: Unix seconds}` from the selected
  profile's state.db. The newest assistant row ID with non-whitespace content and
  `finish_reason != "tool_calls"` (including a missing finish reason) wins. One
  page-level message query includes already-resolved compression ancestors;
  branches remain separate. The timestamp is persisted, not request time.
- Envelope `archived_count`: scoped root count with the same source/cwd/message
  filters, independent of the page/archive selector, using a count query rather
  than fetching archived pages. Existing `total` semantics are unchanged.
- Envelope `inbox_summary_scope: {profile, pending_scope: "process",
  pending_complete: boolean, pending_epoch?: string, replies_complete: boolean}`.
  Reply coverage is **returned rows only**, not every page/profile. Corrupt storage
  does not advertise complete replies. Pending completeness is only this loaded
  supervisor's supported request plane; without that supervisor it is false and
  the epoch is omitted. It never promises unrelated CLI/gateway process coverage.

`session.active_list` retains profile filtering and all existing live fields. Rows
add `pending_kind?`, `pending_count?`, `pending_revision?` with identical attention
semantics and `latest_run?: {run_id, status, at}` for an existing persisted ordinary
`native_session` run. `at` is its `updated_at`; statuses are `starting`, `thinking`,
`usingTool`, `responding`, `waitingForApproval`, `waitingForClarification`, `complete`,
`failed`, `cancelled`. Exact profile + compression root scope is used, never a
branch's parent or canonical Bot Chat scope. The run reader opens the existing
push projection read-only, without delivery configuration, APNs/widget enrollment,
worker startup or mutation. Only live roster rows are covered; this is not a
complete historical run feed. The envelope has `inbox_summary_scope` with the same
process fields, but no `replies_complete`; `profile: null` means unfiltered roster.

`count` counts requests, not clarify questions. Kind priority for mixed supported
requests is approval → clarify → input. `input` covers sudo/secret/vault prompts
(and display-install sudo); it does not assert that the phone can answer them.
GUI reads/actions/tours and unknown methods can retain generic live `waiting`
without any typed attention. No prompt, command, secret, answer or request ID is
included in these summaries. Obtain details/response authority through existing
native chat recovery, and revalidate ownership there.

`revision` is an opaque equality token for the set of supported open requests,
not a monotonic sequence/timestamp/run ID. Answers/cancellations change that set;
new requests get new revisions. `pending_epoch` changes on process restart and
fences process-local reconciliation. These are independent observations, not an
atomic transcript + roster + run cut; a newer event must not be cleared by an
older list response. Omitted attention means no typed request observed in this
process cut (or unavailable runtime plane), **not global "done"**. Omitted reply
means no qualifying durable row observed on this page; omitted latest run means
absent/unavailable projection, never success. Epoch changes, missing runtimes,
failed reads and unvisited pages must preserve unknown/last-good client state.
Authenticated REST/WS access is unchanged; widget bearer tokens still grant only
the exact widget GET path, never these summaries.

## Authenticated registration RPCs

Use the native authenticated WebSocket. The authenticated principal is taken from
its transport, never from client parameters. Stored session IDs must be exact IDs
in the specified existing profile database; titles, foreign IDs, unknown profiles
and Bot Chat lineages are rejected. Compression-only ancestry resolves a stable
root and current tip; normal branches stay separate. Registering needs no active
runtime. A session must already have a durable row (a newly minted, never-prompted
session may not have one).

Common registration parameters:

- `profile`: exact profile name, including `default` for the default profile.
- `stored_session_id`: exact stored root or compression descendant ID.
- `installation_id`, `connection_id`: UUID strings identifying the app installation
  and its saved host connection, not a transient WebSocket ID.
- `environment`: `production` or `sandbox`, enabled by the existing `mobile_push`
  provider configuration.

| RPC | Additional parameters | Result |
| --- | --- | --- |
| `mobile.session_push.register` | `device_token`; optional `categories` (default `["attention","completion"]`), `preview_enabled` (default false) | Registration receipt below |
| `mobile.session_activity.register` | `activity_token`, `activity_id`, `run_id` | Registration receipt below; run must belong to this destination and be within its 8-hour activity lifetime |
| `mobile.widget.register` | `read_token` (64 lowercase hex characters from 32 cryptographically random client-generated bytes); optional `widget_token` (default empty, read-only) | Registration receipt plus `snapshot_path` |
| `mobile.session_push.refresh` | Only installation/connection/environment plus optional device token and categories/preview preference; **no profile or session ID** | `{updated, subscriptions:[{subscription_id,expires_at,categories,preview_enabled}]}` |
| `mobile.session_activity.refresh` | Only installation/connection/environment plus activity token, activity ID, run ID; **no profile or session ID** | Same refresh result |
| `mobile.session_push.unregister` | Only installation/connection and optional `subscription_id` | `{removed}`; alert registrations on the ordinary surface only |
| `mobile.session_activity.unregister` | Only installation/connection and required `subscription_id` | `{removed}`; activity registrations on the ordinary surface only |
| `mobile.widget.unregister` | Only installation/connection and optional `subscription_id` | `{removed}`; ordinary widget registrations only |
| `mobile.session_push.presence` | Profile/stored session/installation/connection plus `foreground` boolean; **no environment or token** | `{foreground,expires_at}` |

A registration receipt is:

```
{
  subscription_id: string,
  expires_at: Unix seconds,
  destination: {surface: "native_session", profile: string, session_id: lineageRootID},
  resolved_session_id: currentCompressionTipID,
  notification_run: null | {run_id, status, started_at, updated_at},
  snapshot_path?: "/api/mobile/widgets/snapshot"
}
```

Registration is an upsert per principal/installation/connection/destination/kind
(and activity ID). Alert/widget leases last 30 days; activity leases last at most
8 hours from the run start. Refresh only rotates existing authorized scopes and
never opens an agent or creates another scope. For widget refresh/token rotation,
call `mobile.widget.register` again for each selected destination, with the same
read token. All scopes on one installation/connection share one read capability;
changing it revokes the old capability for that device/connection.

For alert refresh (`mobile.push.refresh` and `mobile.session_push.refresh`), an
omitted or null `device_token` only narrows existing unexpired subscriptions:
categories become the intersection of stored and requested values, and previews
remain enabled only when both stored and requested values are true. Token and
expiry remain unchanged. The response acknowledges the resulting categories and
preview preference. A later refresh with a valid token may widen preferences,
rotate the token, and renew those retained scopes without reopening their chats.
Registration and activity refresh always require their respective tokens.

Errors: 4403 for unauthenticated transport, 4400 for invalid authority/registration,
4401 for temporary storage failure, 4405 when mobile delivery is not configured.
Undeclared parameters fail contract validation (-32602). Generic session RPCs
retain their existing authentication/attachment policy.

## Producer, payloads and presence

The native supervisor projects events before the transport write, including
frames dropped by detached transports. `HERMES_COMPUTE_HOST_CHILD=1` disables the
child producer; relayed supervisor events own delivery. A runtime's message start
mints one notification run, with its stable destination root. Completion, failure,
approval and clarify produce existing alert/Live Activity payloads and outbox
idempotency. Repeated terminal/finally projections do not produce another alert.
No transcript or reasoning is stored in notification projections unless an alert
preview is explicitly enabled; Live Activity text stays generic.

Alerts carry `hermex.destination` with `version:1`, `surface:"native_session"`,
`installation_id`, `connection_id`, `profile`, `session_id` (root), and `run_id`.
They also carry `hermex.status`, run start/update timestamps, and a hashed event ID.
Do not decode this destination as canonical `surface:"native"` / `canonical_root_id`
or WebUI `surface:"chats"`. Live Activity content-state retains the existing iOS
shape (`sessionID`, `sessionTitle`, `status`, activity/timestamps/final flags).
Use `notification_run.run_id` to register a current activity.

Presence is an explicit authenticated **per-device, per-destination** 60-second
lease and requires that device's alert registration. Renew while the app is
foreground and displaying that session (for example every 30 seconds); clear on
background, navigation away, or transport disconnect. A connected socket alone
is not foreground presence. Lease expiry prevents a crashed phone suppressing
notifications indefinitely. The lease suppresses alerts at enqueue and removes
unclaimed pending alerts/retries at send time. It never suppresses another device,
widgets or activity finalization, and cannot recall an APNs send already claimed.
Canonical Bot Chat delivery/checks remain behaviorally unchanged.

### WebUI soak: exactly one backend owns a migrated destination

Native and WebUI are independent producers/outboxes: there is **no cross-backend
deduplication** and APNs collapse IDs are not an ownership protocol. Before enabling
native push for a Chats destination, stop registering it with WebUI and unregister
its existing WebUI alert and Live Activity subscriptions. End the old local
activity, then register/start the native activity against its new run. WebUI may
continue running for unmigrated features and rollback, but must not execute or
notify the same migrated chat simultaneously. On rollback reverse the handoff:
unregister native subscriptions first, then enable WebUI ownership. Preserve
installation/connection IDs for native logout revocation. No deployment or handoff
is performed by these source changes.

## Least-privilege widget read

`GET /api/mobile/widgets/snapshot` accepts `Authorization: Bearer <read_token>`
only at that exact path and method. It is not registered with any general token
authentication provider, cannot mint a WS ticket, cannot authorize `/api/ws`,
configuration, sessions, or plugin routes, and cannot update tokens or registrations.
POST, path suffixes, and other endpoints fall through to ordinary dashboard auth.
The dashboard Host gate still applies. Responses are `Cache-Control: no-store`;
invalid/revoked credentials return 401, unavailable delivery/storage returns 503.
Only the token hash is stored.

The snapshot is the existing shape:

```
{protocol_version:1, installation_id, connection_id, generated_at,
 items:[{profile,session_id,run_id,status,started_at,updated_at}]}
```

It selects latest run status only for this principal/installation/connection's
unexpired widget registrations, including `native_session` and existing WebUI
`chats` subscriptions. It exposes neither arbitrary sessions nor transcript text.
An unstarted selected session has no run and is omitted. Unregistering the last
widget selection revokes the read capability. Widget push remains the existing
coalesced WidgetKit reload hint, not a second conversation producer.

## Atomic event-published stream cut

`session.resume` now optionally returns `stream_snapshot`. Existing messages,
inflight, and status fields are unchanged and are **not** redefined as atomic.
An attached client can also request `session.stream.snapshot` with
`{session_id: runtimeID, profile?: exactProfileName}`. Another connection or a
mismatched profile is rejected. Both cuts have the same shape:

```
{session_id:runtimeID, stored_session_id, epoch, baseline_seq,
 stream:null | {
   start_seq, segments:[sealedInterimText], assistant:currentSegmentText,
   status, start:messageStartPayload,
   terminal?:messageCompletePayload, reasoning?:publishedReasoning,
   todo_state?:publishedTodoPayload
 }}
```

Event stamping, stream projection and cut copying share a per-runtime publication
lock. The snapshot includes exactly the text published through `baseline_seq`,
not producer `inflight_turn` text (which can run ahead), nor a sequence read after
an unrelated history/DB snapshot. `message.interim` seals a segment;
`message.complete` replaces final text authoritatively. A cold runtime has no
published current turn (`stream:null`); its persisted transcript remains REST/history
owned. The projection is in-memory and does not survive a process restart.

Client ordering:

1. Buffer live frames before resume/cut retrieval; do not auto-resend any prompt.
2. Treat the optional `stream_snapshot` as the authoritative current streaming
   text and set the watermark to its `baseline_seq` for its runtime and `epoch`.
   Do not concatenate legacy inflight text with this projection.
3. Call `session.events.since(session_id:runtimeID,last_seen:baseline_seq)`;
   merge replay and buffered live frames in sequence order, applying each seq once.
4. Ignore seq at/below the baseline or already admitted watermark. An epoch change,
   truncation, gap or overflow requires a fresh resume/history reconciliation and
   fresh cut, not continued replay against the old baseline.

This is a **stream publication boundary**, not a transactional watermark for
REST history, pending request registries, or provider-side writes. Subagent and
process rosters retain their independent reconciliation paths. Durable history
can be ahead of event publication; do not treat a REST fetch as stream evidence.

### Rich stream snapshot v1

`mobile.capabilities.features` includes `rich_stream_snapshot`, with
`feature_versions: {"rich_stream_snapshot": 1}`. `atomic_stream_snapshot` remains
advertised; old scalar fields and their meanings are unchanged. In particular,
legacy `reasoning` still accumulates `thinking.delta`, not model reasoning.

The additive `stream.parts` array is ordered by first publication, with these
three typed shapes (optional fields are omitted when unavailable):

```json
[
  {"kind":"text","text":"Published answer"},
  {"kind":"reasoning","text":"Model reasoning","complete":true},
  {"kind":"tool","tool_id":"call-1","name":"terminal","status":"complete",
   "context":"pwd","preview":"pwd","args_text":"{\"command\": \"pwd\"}",
   "labels":[],"duration_s":0.5,"summary":"done","error":false,
   "inline_diff":"short display excerpt","todos":[]}
]
```

Text deltas append to the open contiguous text part. Reasoning or a tool event
starts a new text boundary. `message.interim` seals text; an already-streamed
interim never appends its duplicate text. A nonstreaming interim supplies its
text directly. Reasoning deltas append to the open contiguous reasoning part;
`reasoning.available` replaces that open block and sets `complete:true`, or
creates a completed block when no block is open. Text/interim/tool events seal
an open reasoning boundary without claiming it complete.

Tools upsert in place by exact `tool_id`; completion without a start creates a
completed part. Repeated starts merge display metadata without resurrecting
completed tools. Full result objects are not copied. A bounded `args_text`
summary is supplied from `args` if explicit args text is absent. `error` derives
from the existing tool failure display predicate (false `success`/`ok`, nonempty
error string, nonzero integer exit code). `todos`/`labels` copy only the fields
published on the event, never fetch producer state or infer task completion.

`stream.thinking` is the latest thinking delta text or generating tool name,
not model reasoning and not a rich part. Terminal completion retains parts;
`terminal`/`status` and authoritative scalar final `assistant` govern settlement,
not a concatenation of partial parts. `todo_state` remains the separate latest
published plan. `message.start` resets the whole projection and its trim flag.
Detached sessions keep projecting through the same publication lock.

Limits apply to the **additive rich projection**, not the legacy scalar fields:
200 parts, 64 KiB UTF-8 per text/reasoning part, 512 KiB serialized UTF-8 for the
parts array (`json.dumps(..., ensure_ascii=False)`). Oldest parts are evicted;
text/reasoning retains its newest UTF-8 suffix. Each display string (including
args summary and inline diff) retains at most 4 KiB; labels/todos lists retain
at most 64 KiB serialized each. Thinking retains at most 4 KiB. Rich-part
trimming sets `parts_incomplete:true` until the next message start. The total
snapshot can exceed 512 KiB because compatible scalar fields are not truncated.
Clients must mark incomplete rich reconstruction honestly, not invent cards.

### Profile-scoped display-only live roster

`session.active_list({profile:"ops"})` resolves an existing profile and filters
this dashboard process's live records. An unknown profile returns 4064, never
creates a home or falls back. Omitted/empty profile preserves all-profile
behavior. Rows add optional `profile` (name, never path), derived from the
record's effective home, including a named launch profile. Custom homes not
recognized by the existing name mapper may report null. Finalized records are
excluded; detached records remain visible. Status precedence remains waiting,
starting, working, idle. Enumeration neither attaches nor focuses sessions;
it grants no control authority and says nothing about workers in other processes.
