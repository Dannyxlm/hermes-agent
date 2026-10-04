# CloudSeed Mobile (Hermex native migration U8)

CloudSeed-owned overlay in `Dannyxlm/hermes-agent`, not an upstream/vendor plugin.
Backend-only native dashboard plugin, mounted at **`/api/plugins/cloudseed_mobile`**.
No WebUI imports, proxying, execution runtime, credential store, scheduler or activation.
The directory name, manifest name and URL use an underscore (not `cloudseed-mobile`).

## Governance and activation prerequisites

Ship this directory with the reviewed immutable Hermes artifact. Native dashboard
startup discovers bundled `dashboard/manifest.json`; bundled APIs mount unless
`cloudseed_mobile` is in `plugins.disabled`. `plugins.enabled` is **not required for
bundled** dashboard APIs. A user-installed governed adapter must explicitly be in
`plugins.enabled` and not `plugins.disabled`; project plugins never mount Python.
This implementation imports `plugins.cloudseed_mobile.*` from the bundled source,
so a loose copy to a user plugin directory is not a standalone installation.
A user adapter must target the matching immutable bundled artifact; do not shadow
it with mutable code. No generic plugin-manager hooks or model tools are registered.

The plugin fails closed until the selected profile's native config contains:

```yaml
cloudseed_mobile:
  owner_provider: basic
  owner_user_id: <exact native authenticated session user_id>
```

Use Hermes's governed config command/deployment configuration, not hand edits to
live config. Discover the actual native provider/user identifier at activation;
never assume the WebUI username/principal is equal. Every route requires the native
`request.state.session` matching both configured identifiers. No-auth loopback,
service-token exemptions and another logged-in user are rejected. Bearer login uses
the native dashboard authentication gate; cookie writes additionally require an
Origin equal to the server's request base URL (no forwarded-host trust). Cookie
requests behind HTTPS reverse proxies must have correct trusted scheme/host handling;
the native iPhone should use authenticated bearer requests.

Native mounting supplies request profile/home/secret scope. Select named profiles
with the native **`?profile=<name>`** query; mobile JSON `profile` must match it.
JSON alone does not select a profile. Owner configuration is profile-specific.
Shared-root credential pools are inventory/usage only from a named profile; reorder
from the root profile. Never read production store contents during development.

Deployment still needs reviewed release/approval, authenticated route verification,
real-device acceptance and a later skill/consumer update. This change does not restart,
install, activate, change nginx, delete data or retire :8787. Ensure nginx accepts the
bounded 1 MiB mobile JSON body (individual previews are at most 256 KiB), and review
foreground sync rates independently of chat rate limits.

## Data and pairing continuity

Photo schema and storage remain `<Hermes root>/webui/photo-catalog/catalog.sqlite3`;
reminders remain `<Hermes root>/webui/iphone-reminders/outbox.sqlite3` (production:
`~/.hermes/webui/...`). They are root-scoped across named profiles, with profile and
device checks in the retained schemas. Do **not** move or delete `~/.hermes/webui`.
Native profile `auth.json` (or its native shared-root fallback) remains the only
credential authority. Memory files are profile `<home>/memories/MEMORY.md`,
`<home>/memories/USER.md`, `<home>/SOUL.md`.

Native principal is SHA256 of a versioned provider/user tuple. Existing WebUI device
rows require **explicit owner-authenticated `register` with `rebind: true`**, the
same device UUID, same profile and original pairing secret. It verifies the stored
secret hash before changing only the principal. It retains catalog/epoch/request
UUID/state, does not resend completed commands, and cannot revive disabled tombstones.
Normal register never silently transfers an existing row. Unknown `rebind` devices,
wrong secrets/profiles and disabled devices fail. New registration requires the
configured owner and a new device UUID/secret. After disconnect, pair with a **new**
device UUID; do not revive the tombstone. Client keychain pairing may be scoped to
server URL; U12 must intentionally transfer existing pairing credentials when the
hostname changes, or explicitly re-pair and account for orphaned pending commands.

Reminders remain foreground-delivered EventKit operations. `processing` may be
redelivered with the SAME UUID while unexpired; the phone's durable executor must
suppress duplicate side effects. Expired queued operations report `expired`, expired
processing operations report `unconfirmed` and never poll again. A late device ack
may settle processing uncertainty. Disconnect cancels queued commands and fences
processing as unconfirmed, clearing their payloads; disabled devices cannot ack.

## HTTP contract for iOS U12

All paths below append to `/api/plugins/cloudseed_mobile`. Errors are sanitized:
401 login required, 403 owner/profile/origin fence, 400 invalid/unavailable device or
payload, 413 body/memory limit, 409 account CAS conflict, 503 storage unavailable.
Photo storage pressure returns 507 `{error, code:"storage_low"}`. Unknown paths 404.
Success wrappers for photos/reminders include `{ok:true, protocol_version:1}`.

### Photos — POST `/photo-catalog/<operation>`

Common JSON: `{profile, device_id, secret}`. Device is canonical lowercase UUID;
secret, asset IDs and fingerprints are 64 lowercase hex characters. `capabilities`
needs only `{profile}`. Read operations still require a registered, enabled,
owner/profile-bound device; status/search/preview do not require its pairing secret,
matching the prior owner-read contract.

| Operation | Additional request fields | Success response beyond common wrapper |
|---|---|---|
| capabilities | none | none |
| register | optional `rebind:true` | catalog stats below |
| begin | `count` integer 0–10,000,000 | `epoch` UUID |
| manifest | `epoch`, `items:[{id,fingerprint}]` (at most 500, unique IDs) | `missing:[asset_id]` |
| put | `epoch`, `item:{id,fingerprint,created,albums,text,favorite,screenshot,preview}` | none |
| finish | `epoch` | catalog stats |
| disconnect | none | none (catalog removed, device tombstoned) |
| status | none | catalog stats |
| search | `query:{text?,after?,before?,favorite?,screenshot?,limit?,cursor?}` | `items`, `next_cursor`, `catalog`, `search_kind:"metadata_and_ocr"` |
| preview | `asset_id` | `preview` base64 JPEG |

Catalog stats: `{id,count,total,storage_bytes,last_sync,complete,epoch}`.
Search items: `{id,created,albums:[string],text,favorite:0|1,screenshot:0|1}`.
`created`/date filters use finite Unix seconds, `after` inclusive, `before` exclusive;
limit defaults 30, range 1–100. Cursor binds device, epoch and complete query (including
limit); changed epoch/query requires restart. New `begin` fences stale writes and
visibility; finish requires the full expected manifest, removes absent assets, and
reports incomplete if previews are still missing. Manifest retries are idempotent.
Preview is JPEG <=262144 bytes, OCR <=65536 UTF-8 bytes, albums <=100, name <=1000
characters. Put refuses storage below 5 GiB plus three times preview size.

### Reminders — POST `/iphone-reminders/<operation>`

Common JSON: `{profile,device_id,secret}`. `status` accepts `{profile}` for the phone's
pre-pair capability probe; if a device ID is supplied it must verify registration.

| Operation | Additional fields | Success response beyond common wrapper |
|---|---|---|
| status | none | none |
| register | optional `rebind:true` | none |
| poll | none | `command:null` or `{id,operation,payload,expires_at}` |
| ack | `request_id`, `state:"completed"|"failed"|"unconfirmed"`, `result:{...}` | none |
| disconnect | none | none |

Local CLI enqueues create/list/complete requests using stable UUIDs, 24-hour expiry,
100 live requests/device maximum. Exact repeated enqueue/ack is idempotent; changed
payload/result under the same UUID is rejected. Ack must follow poll delivery and
be <=256000 serialized UTF-8 bytes. There is no remote HTTP enqueue endpoint.

### Accounts

- GET `/provider/accounts`: `{ok,profile_id,generated_at,providers:[...]}`.
- GET `/provider/accounts/usage?provider=anthropic|openai-codex&refresh=1`:
  same inventory shape with each account's quota windows; explicit bounded probe,
  never inference, credential seed/heal/refresh or token fallback.
- POST `/provider/accounts/primary`: exact JSON fields
  `{provider,account_id,profile_id,revision}`; same inventory shape after native
  `CredentialPool.move_entry` and persisted readback, stale revision yields 409.

Provider section: `{id,display_name,strategy,revision,scope,scope_label,
can_set_primary,read_only_reason,primary_id,accounts}`. Account:
`{id,label,priority,is_primary,status,retry_after,can_set_primary,read_only_reason,
usage:{status,fetched_at,stale,refreshing,windows,message}}`.
Usage window: `{label,used_percent,remaining_percent,reset_at,detail}`.
Labels are deliberately generic `Account N` (no saved labels/emails/token fragments).
Only fill-first pools can reorder; dead/fixed-position accounts and borrowed root
pools are fenced. Native cross-process auth lock and order-generation reconcile
concurrent writers. Usage has four workers, bounded 128 in-flight/cache entries,
60s TTL, 15s explicit-refresh retry fence, credential-identity keys and order epochs.
Usage route waits at most 20s; metadata does not wait. External exception contents
never reach the phone. Background probes operate only on copied account rows and
native HTTP/parsing helpers, never context-unbound credential resolution.

### Memory

- GET `/memory`: `{memory,user,soul,memory_path,user_path,soul_path,
  memory_mtime,user_mtime,soul_mtime,project_context,project_context_path,
  project_context_name,project_context_workspace}`. Missing files return empty text
  and null mtime. Project-context fields are intentionally empty: not an arbitrary
  workspace reader and not supported by this editor.
- POST `/memory/write`: `{section:"memory"|"user"|"soul",content:string}` →
  `{ok:true,section,path}`. Limit 256 KiB UTF-8 per file, 1 MiB whole request.

POSIX directory-fd/O_NOFOLLOW handling refuses target and memory-directory symlinks,
nonregular files and oversized reads. Writes use private same-directory temporary
files, fsync + atomic replace + directory fsync, not in-place truncation. This plugin
currently targets the Linux Ava host; POSIX memory editing is not Windows-portable.
Changes become agent context according to native next-session memory/SOUL behavior;
no mutation of an existing conversation's cached prompt is performed here.

## Relocated owner-local CLIs and skills

Use the native Hermes release's Python to run:

```
<release>/plugins/cloudseed_mobile/scripts/photo-catalog --state-dir <root>/webui --profile default devices
<release>/plugins/cloudseed_mobile/scripts/iphone-reminders --state-dir <root>/webui --profile default devices
```

Same options as WebUI: photo `devices|status --device|search --device --json-file|
preview --device --asset`; reminders `devices|request --device --id --operation
create|list|complete --json-file|status --device --id`. Preserve private JSON files
and preview exports. CLIs refuse absent stores instead of creating a new pairing;
no HTTP service is needed. Activate new discovery/runtime routes for consumers before
retiring WebUI. `skills-patch/*.patch` and complete `*.SKILL.md` contain the proposed
owner skill changes; **not applied** to `~/.hermes/skills` by this commit.

## Session side-task activity

`GET /api/plugins/cloudseed_mobile/session-activity?profile=<name>&stored_session_id=<id>`
requires both query fields and the existing configured owner login. Native mounting
validates an existing profile and binds its request scope; no profile or session DB
is created. Response:

```json
{"version":1,"profile":"default","stored_session_id":"stored-id","epoch":"opaque-replay-epoch","complete":true,"tasks":[{"task_id":"bg_example","kind":"background","status":"running","started_at":1791130000.0}]}
```

Kinds are `background|btw`; preview restart is excluded. Status is
`running|completed|failed|failed_start`; terminal rows add `finished_at` (Unix
seconds). The registry captures profile home and durable parent at admission and
survives retirement/remint of the live parent. Reads follow compression-only
ancestors/continuations using the existing lineage SQL against `state.db` opened
`mode=ro`; absent DB means exact-ID lookup. Branch ancestry is not ownership.
No task prompts, questions, result/exception text, cwd, paths or secrets are sent.

Coverage is this dashboard process's current replay epoch, not external processes
or historical tasks. Terminal retention is 50 per exact admitted parent, six-hour
TTL, pruned on registry operations. Running tasks are never age/count-pruned.
`complete:true` means a complete snapshot of that retained registry scope, not
complete history. Epoch change or missing/expired evidence must read as unknown,
never successful completion. Metadata grants no attach/close/steer authority.
Existing completion events still carry their original result text to their owner.
Errors: 401/403 owner/scope, 400 malformed query/profile, 404 missing profile,
422 absent required query fields, 503 unreadable/corrupt existing session store.

## Tests

Fixture-only tests protect API lifecycle, rebind/tombstones, pagination, provider CAS,
symlinks/size bounds, profile isolation and the relocated CLIs. No production store
contents are read. Runtime Python lacks pytest/FastAPI in this environment, so a scratch
venv supplies test packages via PYTHONPATH without modifying immutable runtime:

```
HERMES_HOME=/home/ubuntu/.hermes/cache/scratch/u8-test-venv/session-home \
PYTHONPATH="$PWD:/home/ubuntu/.hermes/cache/scratch/u8-test-venv/lib/python3.14/site-packages" \
PYTHONDONTWRITEBYTECODE=1 \
/opt/cloudseed-immutable/hermes-venv/current/bin/python -m pytest \
 tests/plugins/test_cloudseed_mobile.py \
 --basetemp=/home/ubuntu/.hermes/cache/scratch/u8-test-venv/fixtures \
 -p no:cacheprovider -q
```

The scratch venv prefix is recognized by the existing home-I/O guard as interpreter
installation; the guard remains enabled. Synthetic HERMES_HOME and every integration
fixture are under that scratch prefix. Verification: **21 passed** with this
command, including native discovery/import/mount and real native request-profile
scope, root-borrowed account write denial, owner/cookie fences and CLI subprocesses.
Removing the reminder secret guard made all four device-route regressions fail
(200 instead of 400); restored guard is green. Source AST comparison confirmed both
SQLite schema strings and every retained domain method are unchanged; only `rebind`
was added. Skill patches applied to scratch copies match the complete replacement
files. Native authentication middleware, real subscription usage and real phone
acceptance still require verification on the sealed candidate before cutover.
