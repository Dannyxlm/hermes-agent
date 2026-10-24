# Bundled optional plugin batch

This release contains four reviewed catalog plugins. Each bundle keeps its upstream license, immutable source revision, SHA-256 file map, fetched-tree digest, and any local changes in `PROVENANCE.json` and `UPSTREAM.patch`.

| Plugin | Version | Verdict | Available behavior |
| --- | --- | --- | --- |
| draft-cards | 0.1.0 | SAFE-WITH-CONFIG | Three draft tools and a draft-review backend. No mail or messaging sender. |
| redaction-pack | 1.4.0 | SAFE | Additional credential masks through the additive redaction API. |
| profile-picker | 1.1.0 | SAFE-WITH-CONFIG | Desktop composer contribution. Python registration is a no-op. |
| duty-board | 0.4.0 | SAFE-WITH-CONFIG | Read-only state summaries, `/duty`, `hermes duty`, and a web dashboard tab. |

Two reviewed catalog pins are excluded. `memory-rewind` at `354927184de03cdc4a9e9e48ddb2865c36368b1f` forwards Git trace variables to child processes, which can write outside its store. `context-audit` at `2aaa96aab2c685798b011a5d6479a4a1fa6a033f` keeps its import-time database path after the active profile changes. A corrected pin needs a new review before either plugin can ship.

## Default state

All four plugins are discoverable but inactive until the owner includes their names in `plugins.enabled`. No profile config is migrated or written by this bundle. Explicit entries in `plugins.disabled` take precedence.

Advanced delegation uses `kind: backend` because registering a provider does not select it. These plugins have no separate provider selector. Their manifests therefore keep the ordinary standalone opt-in gate instead of misclassifying tools or hooks as backends. This preserves the precedent's inactive-by-default outcome.

The two dashboard manifests also set `default_enabled: false`. The generic dashboard gate enforces that flag before backend import, asset delivery, and every subsequent request. Existing bundled dashboards keep their prior defaults. Desktop modules set `defaultEnabled: false` if an owner later materializes their reviewed bytes in the local app.

## Activate after release review

The release owner merges the desired names into each intended profile's existing `plugins.enabled` list through the managed configuration workflow. Preserve unrelated entries and clear any matching explicit disable only when intended. Do not run a catalog install or copy a user plugin over a bundled copy. The normal `hermes plugins enable` transaction invokes dependency admission and is not an immutable-release activation mechanism.

The compatibility tests bind the target runtime identity to 0.22.5. An unstamped source checkout can derive an older version from this fork's historical tags and refuse the plugins' upstream `requires_hermes` constraints. Keep those constraints intact. The release builder must provide the intended runtime identity rather than lower the plugin requirements.

Additional activation conditions apply:

- Draft Cards stores recipient addresses, subjects, bodies, revisions, session IDs, and approvals in that profile's `plugin-data/draft-cards/drafts.db`. Treat the database and backups as private. Its status tool records reported outcomes. It does not enforce approval on other mail tools, and a card approval is not a substitute for the sender's existing authorization checks.
- Duty Board requires an owner in `duty-board/settings.json` before messaging summaries are available. Keep `hide_other_people_content` enabled and do not opt into full shared-channel summaries. Local dashboard and CLI access can display message snippets. The database is opened with `mode=ro` and `query_only=1`. Only dismissal IDs and timestamps are persisted to `duty-board/dismissed.json`. Snippets exist in a short-lived in-memory response cache, not a second transcript store.
- Redaction Pack adds masks. It neither changes existing masks nor enables the global redactor when the owner has disabled it.

## Desktop compatibility and remaining gap

The Electron Desktop app already implements plugin panes, routes, tabs, composer contributions, transcript directives, profile routes, and scoped plugin REST calls. No new Desktop UI is part of this change.

Draft Cards needs `TRANSCRIPT_DIRECTIVE_AREA`, `ctx.rest`, `ctx.onEvent`, `ctx.onDispose`, React Query, and `host.composer`. Profile Picker needs `COMPOSER_AREAS.leading`, `host.newChat`, `host.profiles.list`, profile-route lookup, and focused-session atoms. These APIs exist in this tree.

The app's disk loader scans its local `desktop-plugins` root. Its unified-package reconciler only copies Desktop halves from local profile `plugins` directories. It does not discover this Python release's source-tree bundles or fetch remote server bundles. The reviewed Desktop files are shipped here but are not automatically visible in the app. A separate reviewed app-packaging change must materialize or bundle these exact files before their opt-in toggles and UI can appear. Do not install a second mutable catalog copy as a workaround.

Duty Board ships the web dashboard IIFE contract, not the Electron Desktop ESM contract. Its `/duty` tab works in the web dashboard after activation. It has no Electron-native pane or tab. A native adapter would be separate work.

## Verify and roll back

Run `scripts/run_tests.sh` with the explicit files `tests/plugins/test_bundled_plugin_batch.py` and `tests/plugins/test_bundled_dashboard_consent.py`. The tests use disposable homes and synthetic state, never a live `state.db`.

Before activation, verify the four plugins appear as inactive and that neither dashboard API is reachable. After activation, create and revise a synthetic draft without sending it, verify an added token format is masked, and request a Duty Board count summary. Desktop visual testing remains blocked on the packaging gap above.

To deactivate, remove only the selected plugin names from the owning profile's enabled list or add them to its disabled list through the managed workflow. Disable any separately materialized Desktop halves in the app. Keep private draft and dismissal data unless the owner explicitly approves deletion. The release owner can also revert the bundle commit and rebuild the immutable artifact.
