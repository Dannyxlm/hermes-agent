# Desktop update notices and Codex handoff

Keep the native update badge, checks, and release details visible for CloudSeed builds. Add a clear Update through Codex action that copies an explicit request with version context; copying must not claim delivery or start deployment. Reuse the existing clipboard bridge, update overlay, Git comparison cache, and release process.

- Show bounded upstream commit details from the existing local comparison cache; preserve counts and stale/error semantics if notes cannot be read.
- Keep normal app release notes and add official change links in the upstream/managed views. Only public official revisions belong in official GitHub links.
- Route managed update UI actions to the handoff instead of auto-applying; retain candidate tooling as secondary functionality.
- Validate using focused Desktop UI/store/Electron tests, typecheck, and an isolated rendered preview. No deployment, application replacement, or restart in this change.

## Validation and delivery

Implemented on `codex/desktop-update-notice`, based on publication commit `c1f33edcd3`. Focused UI/store/Electron tests: 180 passing across seven files. Desktop typecheck passes. Scoped ESLint has no errors; three pre-existing padding warnings remain in the Electron comparison tests. Real temporary Git repositories prove the commit details match the compared history and a notes-read failure preserves the proven count.

An isolated Chromium preview exercised the actual components and CSS, copied the request, and reached controls at a 600 × 480 viewport, with no page errors. The preview uses sample revisions/change descriptions. Its temporary server and fixture were removed afterward. No remote publication, deployment, installed-app replacement, or restart was performed.

The handoff copies a request for the user to paste into Codex; it does not launch Codex or submit a chat automatically. The native notification and status checks remain. This change can be included in the next authorized paired release.
