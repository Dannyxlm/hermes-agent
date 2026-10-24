# Security Policy

## Supported versions

Security fixes go into the latest release only.

## Reporting a vulnerability

Please **do not** open a public issue. Report it privately through GitHub's **Report a vulnerability** button on the [Security tab](https://github.com/xiaoyaner0201/hermes-duty-board/security/advisories/new).

Please include affected versions, reproduction steps, and the impact you observed. Expect an acknowledgement within 7 days.

## Scope and design notes

What the plugin is designed to do, so reports can focus on deviations from it:

- The API is served by the Hermes dashboard under `/api/plugins/duty-board/` and sits behind the dashboard's authentication gate. Do not expose the dashboard publicly without authentication.
- It opens `<HERMES_HOME>/state.db` read-only (`mode=ro`) and reads `cron/jobs.json` and `gateway_state.json`.
- Only dashboard dismissal actions write `<HERMES_HOME>/duty-board/dismissed.json`; chat/CLI commands never write or dismiss.
- `/duty` in messaging platforms requires a configured owner and Hermes 0.21.7+ session binding. Unauthorized callers must be refused before collection. The local CLI/TUI is trusted as the local account that can read that Hermes home.
- Full chat summaries reveal conversation titles, places and ages (plus Discord web links on Discord) to the owner in a one-to-one DM. Shared channels, groups, threads and group DMs receive counts only by default. The caller is identified only from the host's per-call session binding (Hermes 0.21.7+); process environment variables never authorize anyone. In a multi-profile gateway, the routed profile's owner and settings apply. Chat summaries refuse under `plugins.isolation: host`, where the plugin host forwards arguments but not the caller. A one-to-one DM is trusted only on platforms whose adapter proves it; elsewhere, including Matrix, the reply is counts only. Explicit `/duty full` plus literal `chat_full_in_channels: true` reveals those details to everyone at the destination, including metadata from other private conversations; enable only in trusted channels.
- `hide_other_people_content` still applies in summaries and raw CLI JSON. Marked-private items must never reveal content or links. Mention, Markdown, newline or link injection in chat output is in scope.
- It makes no outbound network requests, calls no model, and reads no credentials (`.env`, `auth.json`, keychains).
- Links it renders are built only from numeric platform IDs (`https://discord.com/channels/...` and `discord://-/channels/...`). Any way to make it render a link with another scheme or host is in scope.
- Message excerpts are rendered as text, not HTML. Any way to inject markup through message content is in scope.
