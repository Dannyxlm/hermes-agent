# Card contract v1

Owner: server lane (`feat/hx317-showcard`). Changes go through the server lane and a line in `../../COMMONS.md`; never edit a private copy. Both repos copy these bytes and test them against `SHA256SUMS`.

## Files

| File | What it is |
|---|---|
| `card.schema.json` | JSON Schema (2020-12) for one card: the arguments of one `show_card` tool call. `$defs/receipt` is the tool result. |
| `fixtures.json` | Manifest: every fixture with `expect` (`accept` / `reject`), its kind, what it covers, and for forward fixtures what a v1 reader does (`reader`). |
| `fixtures/valid/*.json` | One card per kind and state (28). The server accepts each. |
| `fixtures/forward/*.json` | Cards a later version may send (7): unknown kind, `v: 2`, unknown variant, and the kinds not in v1 (`draft`, `page`, `music`, `health`). A v1 server refuses them; a v1 reader shows title + `alt` (`reader: fallback`) or the kind's default variant (`reader: default-variant`). |
| `fixtures/hostile/*.json` | Cards the server must refuse (64), including the a5 spike's `compose-stress` payload verbatim. Two are deliberately not plain JSON objects: `not-an-object.json` (an array) and `route-nan.json` (a `NaN` literal). A reader that somehow receives one never shows JSON and never crashes. |
| `SHA256SUMS` | `sha256sum -c SHA256SUMS` from this folder. |

File paths in fixtures live under `/synthetic/cards/`; they are sample paths for the synthetic app profile, not real files.

## Kinds in v1

`table` (variant `sheet` for the full-sheet look), `flight`, `route`, `place`, `weather`, `chart`, `document`, `link`, `gallery`, `compose`. Not in v1: `draft`/decisions, `health`, `music`, `page`.

## Where the schema is open and where it is closed

Closed lists: `kind`, `variant`, action `type`, `tone`, compose block `type`, hero `type`, table column `type`, `glyph`, weather `condition`, route `mode`, `source.origin`. Action objects are closed (no extra fields).

Everything else is open: an unknown field anywhere in the envelope, `source`, `data` or an item is accepted by the server and must be kept by a reader as a raw value (string, number, bool, null, array, object) and ignored for layout. `fixtures/valid/open-extra-fields.json` proves it.

## Rules the schema alone cannot say (the server enforces them; a reader re-checks)

1. **Size:** the card's compact UTF-8 JSON is at most 16,384 bytes. Nesting at most 12 levels.
2. **Numbers** are finite (no NaN or Infinity).
3. **One-line fields** (`title`, labels, cells, names, times...) contain no control characters at all, a trailing newline included. Multi-line fields (`alt`, `note`, `document.quote`, `document.quotes[].text`, `link.description`, compose `text` and `quote`) allow `\n` only. No field may contain bidi controls (U+202A–U+202E, U+2066–U+2069).
4. **Table:** every row has exactly one cell per column; `totals`, when present, too.
5. **Chart** (also `weather.chart` and chart heroes): every series has exactly one value per `x`; at most 400 values in total across series.
6. **Actions:** at most two; no two identical. `directions` and `open_in_maps` only on `place` and `route`. `open_file` only on `document`, or a `table` with `data.file`. `open_url` at most once per card; on a `link` it may omit `url` (it opens `data.url`), elsewhere it needs its own `url`. `ask` places its exact `text` in the composer; it never sends. `open` (also the default tap) opens the card's stage. Copy, share and keep live in the long-press menu and are not actions.
7. **Files** (`document.file`, `table.file`, `link.image`, gallery images, image heroes and blocks) are absolute paths without `..`. The server also checks that each one exists, is a regular file, and passes the Hermes read guard (`agent.file_safety`, the same denylist the Files reader applies). A reader fetches them only from our server with the session's credentials. No field is ever a web image URL.
8. **`alt`** is required for `compose`. For every other kind the server derives it when absent.
9. **Flight** arc ends are whole coordinates: `fromLat` with `fromLon`, `toLat` with `toLon`, or neither.
10. **Document:** `quotes` or `quote`, not both. With `pages`, no `page` (top-level or in `quotes`) is past the last page.

## Revisions

All revisions are additive and optional: every earlier fixture is still valid and byte-identical, and a reader that ignores a new field still draws the card.

- **rev 3** (the fields the approved stills draw): document `quotes: [{label?, text, page, tone?}]` (1–3, label ≤ 60, text ≤ 300); flight leg `fromName`, `toName` (≤ 40), `detail` (the seat line, ≤ 120), `arrivalDetail` (≤ 120), `fromLat`/`fromLon`/`toLat`/`toLon`; flight `airline` (≤ 40), `total` (≤ 24), `price` (≤ 24), `priceNote` (≤ 40), `leave: {time, label?, detail?}` (the step before the first leg); route stop `address` (≤ 160), `detail` (≤ 120); chart `caption` (≤ 120, the line under the headline). Asked by the cards lane: quotes, leg names/detail/coordinates, stop address/detail, caption. Added from `stage-trip`: `arrivalDetail`, `airline`, `total`, `price`, `priceNote`, `leave`. Fixtures: `flight-stage`, `document-quotes`, `route-stop-detail`, `chart-caption`, and nine hostile ones.
- **rev 2:** compose `hero` is `{type: image|map|chart, …}`.

## Receipt (the tool result)

```json
{"ok": true, "card_id": "card_3f9c2a1b7d4e", "alt": "Airport to the hotel: YUL · Montréal airport → Hôtel William Gray, 24 min by car, 21 km.\nhttps://maps.apple.com/?saddr=45.4577,-73.7499&daddr=45.5075,-73.554&dirflg=d", "warnings": []}
{"ok": false, "error": "card refused: 2 problems", "errors": [{"path": "data.rows[1].cells", "message": "has 2 cells; the table has 3 columns"}]}
```

A reader renders the call's **arguments** as the card only when the matching tool result parses and says `ok: true`; otherwise it shows nothing for that call (the model is told to repair and call again). The arguments of an accepted call are exactly the card (the server refuses, it never rewrites), so a reader does not need the receipt to draw, only to know the card was accepted and to use `alt` when the call left it out. The card's identity is the tool call id.

## Capability

1. `mobile.capabilities` lists the feature `cards` with `feature_versions.cards == 1` when the server understands the advertisement. **Today's server rejects an unknown `client.capabilities` key with error 4000**, so the app sends the card advertisement only when that feature is listed, and as its own call, never folded into the `server_requests` call (a refusal there would also drop `server_requests`).
2. The app then calls `client.capabilities {"cards": {"v": 1, "kinds": [...]}}`. The reply's `cards` is the contract version this server will offer (1 when `cards.enabled` is on, else 0).
3. The server offers `show_card` to a session's agent when `cards.enabled` is on, the session is a local chat (source `tui`, `desktop`, `local`, `mobile` or `gui`; never a messaging, cron or subagent session), and a client attached to the session advertised `v: 1`. An agent built before that (Home, opened long ago) is rebuilt **once, at the next turn's admission**: the tool list changes between turns, never inside one (prompt caching), and the turn after the advertisement has the tool. Advertise before sending, and a turn already running keeps the tools it started with.
4. `kinds` is recorded but does not narrow the tool in v1: the app advertises all ten, and a reader that meets a kind it cannot draw shows title + `alt`.

## Live delivery, history, reconnect

- `show_card` is always delivered live as `tool.start` (arguments) and `tool.complete` (arguments + the receipt as `result`), even when tool progress is off.
- History: the call is stored like every tool call (assistant `tool_calls` with the arguments, a tool row with the receipt), so history and compacted history replay it.
- Reconnect snapshot (`rich_stream_snapshot`): a `show_card` tool part carries `card` (the whole arguments, never the 4 KB `args_text` tail) and, once complete, `error` false for an accepted card.
