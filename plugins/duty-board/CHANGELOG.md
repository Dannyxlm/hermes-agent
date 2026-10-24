# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-10-14

### Added

- **`/duty` in chat and `hermes duty` in the terminal.** Both send the board as a short text summary: counts first, then what is waiting on you, what is running, results that never made it back, and cron problems, each with its place and age. In a Discord DM each line also carries a jump link; other chats and the terminal show plain text, and `--json` keeps the raw links. No model is called; the summary reuses the dashboard's read-only collectors. The CLI takes `--full`, `--counts`, `--lang` and `--json`.
- Chat strings and compact durations in all 17 locales, with pinned Python CLDR plural rules.

### Security

Chat summaries carry private conversation titles, so access is strict by default:

- **Owner only.** The caller comes from the host's per-call session binding (Hermes 0.21.7+), never from the process environment. Older gateways refuse. Anyone else gets a one-line refusal, decided before any board data is read.
- **Details only in a one-to-one DM.** Groups, channels, threads and group DMs get counts, unless the owner asks for `/duty full` there and `chat_full_in_channels` is `true`. One-to-one is trusted only where the platform's adapter proves it: Discord, Telegram, Feishu, Google Chat, Mattermost, and Slack IM channels. Matrix and unreviewed platforms get counts.
- **Multi-profile gateways:** the routed profile's home, settings, `DUTY_BOARD_OWNER` and `HERMES_LANGUAGE` apply, never the launch profile's.
- **`plugins.isolation: host`:** chat summaries refuse, because the plugin host does not forward the caller. The host does not register `hermes duty` either; use the dashboard there.
- Mentions are neutralized. Markdown is escaped on Discord and made inert where the adapter converts it (Telegram, Slack, Feishu). Nothing is written to the Hermes home before authorization.

### Changed

- The collectors moved from `plugin_api.py` into a shared, stdlib-only `board.py`. The dashboard API returns the same JSON.

### Fixed

- A delegation or conversation id such as `constructor` no longer shows its card as already dismissed ([#2](https://github.com/xiaoyaner0201/hermes-duty-board/issues/2)).
- Improved Turkish dashboard wording after native-speaker review. Thanks to [@farukttbasar](https://github.com/farukttbasar) for the corrections ([#4](https://github.com/xiaoyaner0201/hermes-duty-board/pull/4)).

## [0.3.0] - 2026-10-14

### Added

- The tab now speaks all 17 languages the Hermes dashboard ships: English, 简体中文, 繁體中文, 日本語, 한국어, Deutsch, Español, Français, Italiano, Português, Русский, Українська, Türkçe, Magyar, Afrikaans, Gaeilge and العربية.
- Right-to-left layout for Arabic. Titles, channel names and quoted messages keep their own direction, so an English reply inside the Arabic UI still reads correctly.
- The tab follows the dashboard's language switch live, without a reload.
- Counts use each language's CLDR plural rules. Durations, numbers and lists are formatted by the browser's `Intl` APIs.
- A translation guide in CONTRIBUTING.md and a *Translation fix* issue template. Catalogs are plain JSON in `dashboard/i18n/`.

### Changed

- The summary API reports section problems as structured `issues` (`{section, code, detail}`), so the UI can translate them. `schema_version` is now 2. The plain-text `warnings` list is kept for older bundles.
- A failed collector reports only the exception class name, never its message.
- The question-mark rule for *Waiting on you* also accepts the Arabic question mark (`؟`), and a question mark followed by a closing quote or guillemet.

### Fixed

- An unsupported locale whose name matches a JavaScript object property (`constructor`) no longer breaks the tab.

## [0.2.1] - 2026-10-13

First public release.

### Added

- Dashboard sections: *Waiting on you*, *You wrote, but no reply arrived*, *Running*, *Background jobs*, *Scheduled jobs*, *Recently completed* and *Standing goals*.
- **Open in Discord** buttons that jump to a thread in the Discord desktop app, landing on your message when its ID is known. A header toggle switches to opening in the browser.
- English, Simplified Chinese, Traditional Chinese and Japanese UI, following the dashboard's language setting.
- Optional `settings.json`: owner, privacy, tunable windows and the list of messaging sources.
- Per-section degradation with a warning banner when Hermes's internal schema changes.
- Fictional demo home (`dev/demo_home.py`) and a local harness for screenshots and UI work.

### Security

- Shared threads are attributed to the sender of the latest message when the gateway recorded one, so another person's words are never shown as the owner's.
- Other people's background-job summaries are hidden along with their goals.
- Rewound (undone) messages are ignored.
- Discord links are built only from ASCII-digit ids.
- Dismiss writes are serialised and capped at 2000 entries.

[Unreleased]: https://github.com/xiaoyaner0201/hermes-duty-board/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/xiaoyaner0201/hermes-duty-board/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/xiaoyaner0201/hermes-duty-board/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/xiaoyaner0201/hermes-duty-board/releases/tag/v0.2.1
