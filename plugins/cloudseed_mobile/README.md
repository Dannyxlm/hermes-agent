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

### Worker discovery without attaching

`GET /api/plugins/cloudseed_mobile/session-activity/batch?profile=<name>&stored_session_id=<a>&stored_session_id=<b>`
accepts 1–50 distinct IDs (1–512 characters each; no NUL or whitespace-only IDs).
The existing single-session endpoint and its response are unchanged. The batch
returns `{version:1, profile, epoch, complete:true, sessions:[...]}` in request
order. Every session has the single-route fields above plus:

```json
{"subagents":[{"subagent_id":"child","status":"running","started_at":1791130000.0}],"subagents_complete":true,"processes":[{"session_id":"proc_example","status":"running","started_at":1791130000.0}],"processes_complete":true}
```

These arrays contain current worker metadata only. Agent status is `running|queued`;
process status is `running`; timestamps are optional Unix seconds. Arrays are null
when their registry cannot be read. A false per-domain completeness flag means
positive returned items remain useful, but missing items cannot be cleared. In
particular, old records lacking captured profile/conversation provenance are not
attributed and make their domain incomplete. No commands, goals, output, file
paths, runtime handles, or control authority are returned.

The active registry cannot enumerate children between construction and admission.
While a scoped delegation call or asynchronous unit is still in progress, its
subagent snapshot is conservatively incomplete. Existing positive rows remain
visible; no queued identity is invented. Completeness returns when those owners
settle, including construction rejection and worker failure.

Worker registration captures resolved profile home and the durable conversation.
Nested delegates inherit that identity from their exact registered parent, and
their processes capture it before the child registry entry can disappear. Process
checkpoints preserve the fields; old checkpoints leave them unknown. Read-only
compression lineage resolves the captured durable key; a branched conversation
does not inherit the original conversation's work. This endpoint reads the same
in-process registries as the native gateway, so the bundled plugin must mount in
that process. An isolated user-plugin host is not a substitute for this evidence.
Snapshot epochs describe the current process, independently of event sequence
numbers. Completeness applies to each queried domain, never global task history.

## Files & deliverables (Hermex R3)

All endpoints below are read-only capabilities on the same owner-guarded router.
`profile` is required and must equal the native request scope. `profile=all` is
unsupported; the native mounting layer rejects invalid/nonexistent profiles.
There is no capability advertisement in mobile RPC: probe this route, and treat
404 as unsupported. No source DB, project, folder or chat is created by discovery.

### Workspace grants and policy

Grants come from the selected profile's `projects.db` active project folders,
including secondary folders. The registry is opened with the native `mode=ro`
URI helper, never its initializing connection factory. Also discover immediate
(non-symlink) subfolders of `cloudseed_mobile.workspace_root`. If absent, derive
that root from registered folders whose parent is named `hermes-workspaces`, or
from the configured `default_cwd` / `terminal.cwd` when its parent is named
`hermes-workspaces`. Otherwise only that configured cwd itself is granted, never
unrelated sibling folders. No host path is hardcoded.
Optional `cloudseed_mobile.generation_roots: [absolute_path, ...]` grants approved
external producer roots to deliverable-ID reads only (not workspace browsing).
Grant configuration is deployment/owner policy, never a client-provided root.

Workspace IDs are opaque SHA256 handles bound to profile home, profile and exact
absolute root. They are not bearer credentials. Favourites never create grants.
Reject absolute client paths, NUL, backslashes, dot/parent components and encoded
paths (including double-encoding). Preserve exact Linux path spelling. Every path
component, including the granted root, is opened using directory descriptors and
`O_NOFOLLOW`; final descriptors must be regular files. Symlinks are deliberately
unsupported even inside a root. Download/stream retain that descriptor rather than
reopening a checked pathname. FIFO/device/directory bytes cannot be exported.

One shared case-insensitive policy covers index references, listing/search and all
byte routes: `.ssh`, credentials, `.env*`, auth stores, config/service-secret trees,
session/state/project DBs, other-profile homes, native managed-file credential
basenames and token/pairing subtrees are denied. Dependency/build/cache trees are
excluded. Sensitive credentials remain denied even with `reveal=1`. Profile-store
symlinks are denied. `outputs/private` / `evidence/private` are excluded from
aggregate/search/list/reads unless explicitly revealed with `reveal=1`. No file
body, thumbnail or background remote-media download is performed during search.
Remote references are metadata only; local/private literal hosts are rejected.
There is no remote proxy/fetch/redirect path. Remote IDs cannot use byte routes;
client explicit remote open must not forward the native bearer off-origin.

### Exact route contracts

Paths append to `/api/plugins/cloudseed_mobile`; query defaults are listed here.

| Method/path | Query | Success |
|---|---|---|
| GET `/workspaces` | `profile` | `{items:[{id,name,root_path,quick_folders:{outputs,plans,reports}}],partial}`; quick-folder values are booleans |
| GET `/deliverables` | `profile, workspace_id?, session_id?, kind?, q="", cursor?, limit=50, reveal=0` | `{items,next_cursor,coverage,index_revision,updated_at,partial,refresh_status}` |
| GET `/deliverables/content` | `profile, id, reveal=0` | `{id,profile,content,version_hash,display_type,not_saved_to_workspace:true}`; inline content only |
| GET `/workspace-files` | `profile, workspace_id, path="", q="", cursor?, limit=50, reveal=0` | `{items,next_cursor,partial,workspace_id,path}` |
| GET `/workspace-files/read` | `profile, workspace_id + path` **or** `profile + id`; `reveal=0` | `{path,text,binary,truncated,byteSize,mime}`; `text:null` for binary/invalid UTF-8 |
| GET `/workspace-files/download` | same target as read **or** `profile + media_path` | original streamed attachment bytes, MIME, Content-Length, `Cache-Control:no-store` |
| GET/HEAD `/workspace-files/stream` | same target as download; optional `Range` header | original audio/video bytes; full 200, single-range 206, unsatisfiable/malformed/multiple-range 416; HEAD has identical headers and no body |

Supplying multiple target forms is 400. `media_path` is an absolute path restricted
to the selected profile's `images`, `screenshots`, `cache` media types and
`attachments` directories, including producer-supported `audio_cache`,
`image_cache`, `video_cache`, `document_cache`, and `browser_screenshots` layouts;
it does not grant profile configuration or arbitrary host paths. The same descriptor and sensitive-name checks apply. This target lets
transcript attachments reopen before/without a deliverables index entry. Generation-root handles are not independently
browseable. File-ID lookup is profile/cache-authorized and then re-applies current
grant, containment and sensitive/private checks at actual open. Deleted/moved
source IDs are tombstones (404); direct workspace browsing shows current files.
No generated revision or filesystem mtime is represented as a delivery timestamp.

Deliverable items carry `schema_version:1`, opaque `id`, `profile`, `kind`
(`file|remote_media|inline_content`), `workspace_id` and `relative_path` (nullable),
`display_name`, `display_type` (MIME), `stored_session_id`, `message_id`,
`source_chat_title`, nullable `observed_at` (saved message Unix seconds), `action`,
`outcome`, `provenance`, `occurrences`, `occurrences_partial`. File items carry
`private` and optionally `availability:"deleted"`; remote items carry `url`;
inline items carry `version_hash` but **never content in metadata lists**.
Occurrences carry `id,stored_session_id,message_id,observed_at,action,outcome,
provenance,source_chat_title`. Actions/outcomes are
`delivered|created|edited|read|referenced|write_failed|pending`. Unknown timestamps
are null, never request-time or filesystem timestamps. Unknown types stay visible.

Write/patch/edit/move/delete/read intent is joined to tool results by tool_call_id;
missing/unrecognized success stays pending and explicit errors never become
production. Native untrusted-result wrappers are bounded. Assistant MEDIA,
markdown local links, producer structured output and qualifying finished HTML/SVG/
code fences are recognized. User imports, reasoning fields/blocks and terminal
prose are excluded. File identities fold cross-chat rows but retain occurrences;
last successful production/delivery drives recency. Default Recent includes explicit
deliveries plus successful production under `outputs`, `docs/plans`, `docs/reports`,
`evidence/summaries`; `session_id` adds touched/read/failed/pending references and
follows native compression-only ancestry/continuations, not arbitrary branch ancestry.

### Cache, bounds and partial semantics

Rebuildable cache: selected profile `<home>/plugin_data/cloudseed_mobile/deliverables.sqlite3`.
`GET /deliverables` reads derived metadata and schedules a bounded FastAPI background
refresh; first cold response is partial, and the next explicit refresh observes
progress. Source `state.db` uses the native read-only helper, never SessionDB.
Source DB/WAL inode/size/mtime changes schedule a scan; they never revoke published
file handles on an unrelated chat write. A completed session projection replaces its
prior projection in one cache transaction. Keysets persist progress through every
session/message batch, including across concurrent source writes. Changed workspace
grants immediately fence old projections; actual file opens also revalidate grants. Canonical source bytes are never modified. Archived/hidden sessions and
inactive rewound messages are excluded. Original stored IDs and durable message UIDs
preserve provenance/dedup across native lineage.

- Metadata pages: default 50, range 1–200. Query text <=256 characters;
  relative paths <=4096; cursors <=8192. Cursors bind profile/home, filters/private
  reveal and index revision (or listing metadata snapshot), and use keyset ordering.
- Cold backfill visits every stored session; 20 session/message work units per
  HTTP-triggered refresh. Each message batch is at most 2000 messages / 4 Mi
  characters (one oversized message still advances). No session or reference-count
  cutoff silently discards the remainder. Individual saved content/tool-call
  strings retain the 1 Mi character parser bound, depth 6 and 512 nodes.
  Refresh/query SQLite budget 0.75 seconds with progress interruption;
  worker refresh additionally accepts a cancellation event. Finished unchanged
  snapshots require no further source scan.
- Occurrence history: <=20 contributing session rows and <=100 occurrences per file;
  any clipping is explicitly `occurrences_partial:true`.
- Workspace listing/search: <=5000 visited entries, depth 6, 0.25-second scan budget;
  workspace discovery caps registry/immediate-root entries at 1000 and generation roots
  at 100. Names/path/type/size/mtime only; `q` searches descendants of `path`.
- Preview: 512 KiB prefix only, preserving binary/truncation/MIME/total-byte-size.
  Original byte responses use 64 KiB chunks without a whole-file/base64 allocation.
  Audio/video stream permits one closed/open/suffix byte range; 416 includes
  `Content-Range: bytes */<size>`. Unsupported stream type is 415.

Coverage is `{indexed_sessions,total_sessions,session_cap:null,refresh_pending,refresh_progress}`.
`refresh_pending` is true while source/grant changes or unfinished batches require
more work, independently of top-level `partial` (which can also mean a permanent
projection limitation). `refresh_progress` is an opaque scan checkpoint digest; it
changes as a long session advances, even before `indexed_sessions` increments.
Clients may continue scoped, cancelable polling while pending, with bounded retries
when the checkpoint stalls. A missing/symlinked source has no runnable refresh work.
The total is
nullable when the source is unavailable, not a fabricated full-history count. `updated_at`
is nullable cache-refresh Unix time, not deliverable time; `index_revision` is
opaque/nullable. `refresh_status` is `cold|refresh_pending|partial|ready|source_missing|
source_unavailable|query_budget_exceeded`. Missing/corrupt source yields HTTP 200
with `partial:true`, not an authoritative empty library. Metadata scanner partial
also means incomplete scope, never “No files”.

Errors use HTTP codes and native `{detail:string}` for policy failures:
401 missing owner login; 403 wrong owner/profile, sensitive/private/symlink/nonregular
access; 400 malformed query/path/target or out-of-range limit; 404 unknown handle/ID,
missing file/profile or deleted source; 409 invalid/cross-scope/stale cursor; 415
unsupported stream MIME; 416 invalid Range (empty body plus Content-Range); 422 native
missing/ill-typed query fields; 503 storage/project-registry unavailable, with existing
sanitized `{error:"Mobile storage unavailable"}`. Preserve last client snapshot on
errors. There are no write, archive extraction, execution or arbitrary URL routes.

## Tests

R3 Files & Deliverables verification uses the requested immutable interpreter and
scratch FastAPI dependency path, with safety guards enabled:

```sh
HERMES_HOME=/home/ubuntu/.hermes/cache/scratch/u8-test-venv/hxr3-files-home \
TMPDIR=/home/ubuntu/.hermes/cache/scratch \
PYTHONPATH="$PWD:/home/ubuntu/.hermes/cache/scratch/u8-test-venv/lib/python3.14/site-packages" \
PYTHONDONTWRITEBYTECODE=1 \
/opt/cloudseed-immutable/hermes-venv/current/bin/python -m pytest -q \
-p no:cacheprovider \
--basetemp=/home/ubuntu/.hermes/cache/scratch/u8-test-venv/hxr3-files-pytest \
tests/plugins/cloudseed_mobile tests/plugins/test_cloudseed_mobile.py
```

Verified **36 new server tests + 25 existing plugin tests**; combined **61 passed**.
The repository harness intentionally relocates basetemp from inside the operator's
Hermes home to a disposable `/var/tmp/hermes-pytest` root and removes it afterward.
Do not disable that protection to force the nominal scratch path. The tests prove
owner/profile/handle/cursor fences, credential/private exclusions, actual descriptor
replacement races, original bytes, Range/HEAD, truthful tool outcomes, read-only
source stores, message-edit/deletion invalidation and bounded cold/query coverage.
A 40-session fixture with 300,000-character inline HTML in every session measured
metadata-page Python peak allocation **15,765 bytes**, query **0.002514 seconds**;
inline bodies are not hydrated by metadata pagination. HTTP contract examples in
the handoff are captured from TestClient fixtures, not live calls or codec evidence.

The earlier U8 migration baseline follows (its test count is historical).

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

Publication preparation also checkpoints hashing and row decoding by keyset,
with cancellation checks between rows. The final cache replacement uses one
transaction and SQL copy so readers never see a partially replaced session.
Its atomic database commit still scales with the completed session size; no
Python materialization of the whole session is required. Additional cache tables
leave the deployed three-column `segments` contract intact for rollback.
