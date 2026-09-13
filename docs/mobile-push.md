# Private mobile notifications

This opt-in adapter projects canonical events into APNs alerts and ActivityKit
updates. Alerts are generic unless the authenticated device opts into previews. It never accepts alert text, a topic, approval answers, or a transcript from
clients. No push-to-start is provided. A visited Bot Chat must be registered before it
receives alerts; this is not an automatic subscription to every profile.

## Configuration

The launch profile's `config.yaml` contains:

```yaml
mobile_push:
  enabled: true
  team_id: 8AA929B9Q5
  key_id: YOUR_APNS_KEY_ID
  private_key_path: /absolute/protected/path/AuthKey.p8
  environments: [production]
```

The key is external to source, readable only by the service user. Never log tokens or
private keys. The only topics are `co.cloudseed.hermex.ava` and its ActivityKit topic.
Registration reports unavailable if configuration or HTTP/2 credentials are missing.
An existing service thread drains a durable SQLite outbox; no new daemon is installed.
Disable the configuration and restart to stop sends; deleting the isolated outbox is
unnecessary for rollback. Removing a connection or signing out must unregister first.
Device subscriptions expire after 30 days, ActivityKit subscriptions after eight hours.

## Native RPC

All methods require the server-minted authenticated WebSocket identity. Register
methods additionally validate the existing attached `mobile` scope:
`profile`, `canonical_root_id`, `session_id`. Client UUIDs select a destination, never
authorize a profile. Unsupported/disabled configuration returns error 4405.

* `mobile.push.status({})`: `{available, protocol_version: 1}`.
* `mobile.push.register(scope + {installation_id, connection_id, device_token,
  environment, categories?, preview_enabled?})`: `{subscription_id, expires_at}`. Both IDs are UUIDs;
  categories are `attention` and/or `completion` (both by default; empty disables
  alerts). Re-register to rotate a token or refresh its expiry.
* `mobile.push.unregister({installation_id, connection_id, subscription_id?})`:
  `{removed}`. Clears this principal's matching device and activity subscriptions
  and unsent jobs. It works after the runtime has been reaped.
* `mobile.activity.register(scope + {installation_id, connection_id, activity_id,
  activity_token, environment, run_id})`: `{subscription_id, expires_at}`. Independent
  of alert permission. The `run_id` must match `notification_run.run_id` returned by
  `mobile.snapshot` / `mobile.open`. A run that ended during token registration gets
  a terminal `end` update. Re-registering rotates that activity's token.
* `mobile.activity.unregister({installation_id, connection_id, subscription_id})`:
  `{removed}` for the owned activity only; no attached runtime required.
* `mobile.push.refresh({installation_id, connection_id, device_token, environment,
  categories?})` rotates existing live alert subscriptions for this authenticated
  principal and connection without attaching runtimes. Each stored profile/root
  must still be a valid canonical chat. No new, expired, revoked, or removed scope
  is created. Valid alert leases renew for 30 days; categories apply to every match.
* `mobile.activity.refresh({installation_id, connection_id, activity_id, run_id,
  activity_token, environment})` rotates only the matching existing activity and
  replays its latest state, including a terminal end. Its original expiry remains.
  Both refresh methods return `{updated, subscriptions: [{subscription_id, expires_at}]}`.
  The existing environment must match. Use these methods on cold launch/token
  changes before reopening any chats; ActivityKit remains independent of alerts.

`notification_run` is null before any observed run, otherwise contains `run_id`,
`status`, `started_at`, `updated_at` (Unix seconds). A persisted opaque run ID is
created at `message.start`, before the transport delivery path, and survives restart.

Native alerts carry `hermex.destination = {version: 1, surface: "native",
installation_id, connection_id, profile, canonical_root_id, run_id}`. Taps must
validate the installed connection and authenticate before reopening that exact root.
An approval alert only opens the app; the existing approval RPC remains authority.
The top-level `event_id` is a stable SHA-256 digest of the persisted canonical event
identity. Use it for device-side duplicate suppression across retries/restarts.

## Shared adapter API

Import `Scope` and `PushService` from `tui_gateway.mobile_push`; use
`service_for_home(launch_home)` for the configured process-owned service. The shared
instance is thread-safe. Independent processes may share its SQLite file because
outbox claims are leased transactionally.

`Scope(surface, profile, session_id)` accepts `native` or `chats`; native session_id
is the canonical root. The caller must authenticate and validate this scope before
registration. WebUI must perform its own cookie/trusted-auth + CSRF + profile/session
visibility checks. The store is not an authorization bypass.

* `service.register(principal, scope, installation_id=..., connection_id=...,
  token=..., environment=..., kind="alert", categories=("attention","completion"),
  activity_id="", run_id="", preview_enabled=False)` returns the registration receipt.
* `service.unregister(principal, installation_id=..., connection_id=...,
  subscription_id=None, kind=None)` returns a count.
  For logout even when the provider is disabled, import and call
  `unregister_for_home(home, principal, **same_owned_ids)`.
* `service.refresh(principal, installation_id=..., connection_id=..., token=...,
  environment=..., accepts_scope=..., kind="alert", categories=...,
  activity_id="", run_id="", preview_enabled=False)` updates live owned rows only. The mandatory
  `accepts_scope(Scope)` callback validates current authorization without opening
  a runtime. Version comparison prevents a concurrent unregister or replacement
  from being resurrected after validation.
* `service.start_run(scope, run_id=None, event_id=None)` persists or reuses an opaque
  run; WebUI supplies its journal run ID to retain `run_id:seq` dedupe.
* `service.record(scope, run_id, event_id, status, preview=None)` accepts only generic statuses:
  starting, thinking, usingTool, responding, waitingForApproval,
  waitingForClarification, complete, failed, cancelled. Attention IDs must be stable
  request IDs within a run. No network occurs in record/register.
* `service.current_run(scope)` returns the current persisted run or null.
* `service.drain_once()` performs one bounded delivery batch; `close()` stops the
  worker. Tests inject a sender and a clock and use real temporary SQLite.

APNs `aps.timestamp`, `stale-date`, and `dismissal-date` use Unix seconds. ActivityKit
`startedAt` and `updatedAt` use seconds since 2001-01-01. ActivityKit stays generic. Alert registration/refresh accepts a strict boolean
`preview_enabled` (default false). When true, `AlertPreview` carries the canonical
chat/bot name (80 characters) and completed assistant reply excerpt (240 characters).
The producer supplies this text, never the client. Tool output, reasoning, approval
commands and clarification questions are not used as reply excerpts. Markdown links
become readable labels; code blocks are omitted. Missing text or oversized payloads
use the generic fallback. Preview text lives only in the bounded private alert outbox;
the run status projection stays content-free. Turning previews off governs queued
alerts at delivery too. The additive SQLite field defaults existing subscriptions to
false and is compatible with the previous release. Apple acceptance proves provider transport only; actual notification taps
and suspended-app activity updates need separate device evidence.

Progress coalesces for ten seconds. Attention and terminal updates bypass that delay,
with at most one ActivityKit dispatch per subscription per real clock second. APNs
timestamps are stamped at dispatch rather than incremented on each model event.
Every newer activity state supersedes older pending activity retries, so resuming
after approval cannot restore an obsolete waiting state. Separate attention alerts
retain their own delivery queue.
Ending an expired ActivityKit projection does not mark the underlying agent run done.


## Widget delivery and limited reads

The optional push store supports Chats widget subscriptions with an independent,
hashed, thirty-day reader credential scoped to principal, installation and
connection. Only fully authenticated callers may grant an existing Chats scope.
The widget reader exposes latest registered run metadata and token rotation,
never transcripts, approvals, new scopes, or full app authentication.

Inbox-relevant state transitions use the same durable outbox. Pending widget
reloads coalesce across one device connection and use the fixed APNs widgets
topic with `aps.content-changed:true`. Invalid widget tokens disable push while
retaining authorized snapshot reads. Whole-connection logout revokes the reader.
The additive table preserves old-source reads/writes; rollback restores the
previous server and existing alerts. New widget support is absent on rollback,
and the client keeps its last valid snapshot until foreground setup resumes.
