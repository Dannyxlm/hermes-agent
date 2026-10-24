# Bundled advanced delegation

This fork vendors the official NousResearch plugin version 1.0.0 from
[`hermes-official-plugins@b3825d097195b5748585b0491fab48c95d105c19`](https://github.com/NousResearch/hermes-official-plugins/tree/b3825d097195b5748585b0491fab48c95d105c19/advanced-delegation).
It adds `model_map` and `profile_routes` through the existing delegation-provider
interface. It adds no Python dependencies and makes no model or network calls
itself; delegated children use the selected provider normally.

## Placement, provenance and activation

The bundle is `plugins/delegation/advanced-delegation/`. Core's
`collect_directory_manifests` / `scan_directory` discovers the category directory,
with canonical plugin key `delegation/advanced-delegation`. `gate_manifest` loads
bundled `kind: backend` plugins without `plugins.enabled`; `plugins.disabled` still
wins. The integration test uses that real path without copying a user plugin.

The sole runtime behavior adaptation is **one line** in `plugin.yaml`:
`kind: standalone` becomes `kind: backend`. `cli.py` has two comment-only health
annotations retaining upstream optional-editor fallback boundaries. Upstream tests
have mechanical lint compatibility edits (BOM-tolerant reads, fixture-writer
annotations, redundant-noqa removal, explicit `check=False`, unused local rename);
their assertions are unchanged. No core lint policy or exclusions are relaxed.
All remaining upstream bytes, including the license, are unchanged.
`PROVENANCE.json` records the repository, commit, subdirectory, SHA-256 of every
original upstream file and every adapted file. `UPSTREAM.patch` enumerates the
complete adaptations for independent reconstruction. The upstream README
is retained verbatim; its catalog installation instructions do not apply to this
bundled distribution. `cloudseed/hermes-overlays.v1.json` records this maintained
fork packaging overlay.

The manifest adaptation is necessary for managed-immutable packaging:
`hermes plugins enable` calls `cmd_enable` → `_activate_key` →
`admit_plugin_set_change` → `pm.client.sync_venv`, which refuses dependency mutation
for a managed-immutable source even when the plugin is bundled and dependency-free.
No installer or dependency-ownership check is bypassed. Instead, the same bundled
backend mechanism as `delegation/default` registers these providers at startup.
The managed-enable refusal is covered using a disposable release-marker fixture.

**Default-off means the strategies are not selected.** Registration exposes the
CLI and slash command, but does not write config, add route choices to the default
schema, or change child routing. `delegation.backend` remains `default` (also when
unset). This bundle does not auto-migrate an existing explicit backend selection.

## Post-release configuration and smoke test

Do not run a catalog install or `hermes plugins enable` for this bundle. No
`plugins.enabled` entry is required. After the reviewed immutable release ships,
the operator should:

1. Run `hermes plugins list` and `hermes delegation config --show`. Confirm the
   bundled key is loaded and the mode is still Default. If `plugins.disabled`
   explicitly contains `delegation/advanced-delegation` or `advanced-delegation`,
   stop and resolve that intentional policy through the owning configuration
   workflow; do not attempt an installer workaround.
2. Configure one model route with the existing config CLI, while staying Default:

   ```sh
   hermes config set delegation.backends.model_map.options '[{name: smoke, description: "Approved routing smoke test.", provider: anthropic, model: claude-opus-5-5}]'
   hermes config set delegation.backends.model_map.default smoke
   hermes delegation config --show
   ```

   The provider/model above is an example, not a claim of account access. Substitute
   the approved working route for the delegating profile. Prefer `provider` and
   existing scoped credentials: do not put keys in route configuration, reports,
   or prompts. `hermes delegation config` offers the interactive route editor but
   also selects a mode when finished, so use the explicit commands to stage first.
3. In a disposable **new chat**, `/delegation model_map` selects it for that session
   only. Smoke-test a harmless task without an `option` (uses the configured smoke
   default), and verify its result `route.backend`, `route.label`, provider and
   model. The old session's cached tool schema is deliberately not rebuilt.
4. Only after the smoke passes, run
   `hermes config set delegation.backend model_map`, then start a new session
   (`/new`). Its `delegate_task` task schema should expose `option: smoke`.
   Run a harmless task explicitly choosing it; confirm `route.label: smoke` in
   the result. `/delegation` should list the three modes and their configuration.
5. Roll back strategy selection with
   `hermes config set delegation.backend default` and start a new session.
   `/delegation default` switches an existing session back without persisting.
   The configured model-map routes can remain stored without being active.

A config managed by another package manager (`HERMES_MANAGED` or `.managed`) may
refuse config writes separately from immutable dependency ownership; honor that
configuration authority rather than disabling its guard.

## Profile-route authority

`delegation.backends.profile_routes.profiles` is an explicit allowlist. Listing a
profile authorizes the delegating agent's children to run with that target
profile's credentials and credential pools, model, skills, `SOUL.md` and terminal
policy. This is intended cross-profile authority, **not credential isolation from
the delegating agent**. Review every listed profile before selecting this mode;
never enumerate or read real credential files during packaging tests. Target
profiles need not install this plugin themselves.

To stage an approved allowlist without selecting it:

```sh
hermes config set delegation.backends.profile_routes.profiles '[{name: approved-profile, description: "Approved specialist scope."}]'
hermes config set delegation.backends.profile_routes.default approved-profile
```

Then repeat the session-only smoke/new-session procedure with `profile_routes`
and verify the selected profile in the result. Route descriptions and names are
model-visible, so keep credentials and private identifiers out of them.

## Verification scope

`tests/plugins/test_bundled_advanced_delegation.py` checks real bundled discovery,
both provider registrations, unchanged default routing/schema/result shape,
model-map routing through core result assembly with an offline stub provider,
slash and CLI loading, explicit disabling, and managed enable refusal. The
vendored upstream suite covers editors, validation, profile routes, host mode and
old-core behavior. These offline tests are not a claim that live provider
credentials, actual profile grants, or released routing have been smoke-tested.
