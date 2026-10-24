// Render the real bundle with React for every catalog and check the output.
//
//   node tests/js/render_check.mjs
//
// Needs `react` and `react-dom` resolvable from the repo (npm i --no-save
// react@19 react-dom@19 in CI). Uses a fixed summary so it needs no server.
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const source = fs.readFileSync(new URL("../../dashboard/dist/index.js", import.meta.url), "utf8");
const catalogs = fs.readdirSync(new URL("../../dashboard/i18n/", import.meta.url)).filter((n) => n.endsWith(".json")).map((n) => n.slice(0, -5));
const now = 1_800_000_000;
const conv = (id, extra) => Object.assign({
  session_id: id, title: "Release notes", where: "#projects", source: "discord", who: "", mine: true,
  link: "https://discord.com/channels/1/2", app_link: "discord://-/channels/1/2", dismiss_key: id + ":k",
  since: now - 5400, started_at: now - 600, activity_at: now - 30, reply_tail: "Should I ship it?", ask: "ship it",
}, extra || {});
const summary = {
  schema_version: 2, plugin_version: "test", generated_at: now,
  issues: [{ section: "running", code: "missingTable", detail: "session_turn_leases" }, { section: "cron", code: "collectorFailed", detail: "OSError" }],
  warnings: ["running: table session_turn_leases not found", "cron: collector failed (OSError)"],
  gateway: { platforms: [{ name: "discord", state: "connected" }, { name: "feishu", state: "retrying" }, { name: "custom_bridge", state: "weird" }, { name: "proto_bridge", state: "__proto__" }, { name: "constructor", state: "toString" }] },
  conversations: {
    waiting: [conv("w1"), conv("w2", { since: now - 90000 })],
    unanswered: [conv("u1")],
    running: [conv("r1", { doing: "tool running: terminal" }), conv("r2", { private: true, who: "Ana", doing: "terminal command running (75s elapsed)", stalled: true, quiet_s: 900 }), conv("r3", { doing: "delegate_task: subagent ⏳ waiting on provider — 42s" })],
    done: [conv("d1", { promised: true }), conv("d2")],
  },
  delegations: {
    running: [{ id: "x1", goal: "Collect screenshots", count: 3, dispatched_at: now - 300, session: conv("s1") }],
    dropped: [{ id: "constructor", goal: "Prototype-safe", completed_at: now - 4000, summary: "done", session: conv("s2") }, { id: "x2", goal: "Benchmark", completed_at: now - 4000, summary: "done", session: conv("s2") }],
  },
  cron: {
    enabled: 12, paused: 1,
    problems: [{ id: "c1", name: "Nightly backup", last_at: null, reasons: [{ code: "lastError", detail: "exit 1" }, { code: "deliveryError", detail: "403" }] }],
    running: [], upcoming: [{ id: "c2", name: "Morning briefing", next_at: now + 3600, last_status: "ok" }],
  },
  goals: { items: [{ goal: "Ship v1", session: conv("g1"), turns: 3, max_turns: 1, status: "active" }, { goal: "Docs", session: conv("g2"), turns: 4, max_turns: 20, status: "paused" }], stale: 1 },
};

function render(locale, hostHasI18n = true) {
  let Plugin;
  let stateCalls = 0;
  const storage = { "hermes-locale": hostHasI18n ? "en" : locale };
  const window = {
    localStorage: { getItem: (k) => storage[k] ?? null, setItem: (k, v) => { storage[k] = v; } },
    addEventListener() {}, removeEventListener() {},
    __HERMES_PLUGINS__: { register: (_name, component) => { Plugin = component; } },
    __HERMES_PLUGIN_SDK__: {
      React,
      hooks: { ...React, useState: (init) => React.useState(stateCalls++ === 0 ? summary : init) },
      fetchJSON: () => { throw new Error("render must not fetch"); },
      useI18n: hostHasI18n ? () => ({ locale }) : undefined,
    },
  };
  const realNow = Date.now;
  Date.now = () => (now + 1) * 1000;
  try {
    vm.runInNewContext(source, { window, navigator: { language: "en-US" }, document: { hidden: false, addEventListener() {}, removeEventListener() {} }, setInterval: () => 0, clearInterval() {}, setTimeout, Date, Intl, console });
    return renderToStaticMarkup(React.createElement(Plugin));
  } finally {
    Date.now = realNow;
  }
}

const en = render("en");
const problems = [];
if (en.includes('class="db-item is-leaving db-line"><div class="db-line-main"><div class="db-line-title">Prototype-safe')) problems.push("prototype-like delegation id incorrectly rendered as leaving");
for (const locale of catalogs) {
  const html = render(locale);
  const text = html.replace(/<[^>]+>/g, " ");
  const tag = `${locale}:`;
  if (!html.includes(`lang="${locale}"`)) problems.push(`${tag} root lang attribute missing`);
  if (locale === "ar" && !html.includes('dir="rtl"')) problems.push(`${tag} not rendered right-to-left`);
  if (locale !== "ar" && !html.includes('dir="ltr"')) problems.push(`${tag} not rendered left-to-right`);
  const leftover = text.match(/\{[a-zA-Z]+\}/g);
  if (leftover) problems.push(`${tag} unfilled placeholders ${[...new Set(leftover)].join(" ")}`);
  const rawKeys = text.match(/\b(?:activity|cronReason|issue|gateway|section|platform)\.[a-zA-Z_]+\b/g);
  if (rawKeys) problems.push(`${tag} raw message keys ${[...new Set(rawKeys)].join(" ")}`);
  if (/\[object Object\]|undefined|NaN/.test(text)) problems.push(`${tag} rendered [object Object]/undefined/NaN`);
  for (const english of ["Waiting on you", "Background jobs", "Scheduled jobs", "Said it would follow up", "not found"]) {
    if (locale !== "en" && text.includes(english)) problems.push(`${tag} still shows English "${english}"`);
  }
  if (locale !== "en" && html === en) problems.push(`${tag} identical to English`);
  // User content (titles, places, asks) must be bidi-isolated so it keeps its own direction inside RTL text.
  for (const needle of ["<bdi>Release notes</bdi>", "<bdi>#projects</bdi>", "<bdi>ship it</bdi>", "<bdi>Ana</bdi>"]) {
    if (!html.includes(needle)) problems.push(`${tag} user content not isolated: ${needle}`);
  }
  // The localStorage fallback (older hosts without SDK.useI18n) must pick the same locale.
  if (render(locale, false) !== html) problems.push(`${tag} localStorage fallback renders differently from host locale`);
}
// A locale Hermes may add later must fall back to English, not crash.
try { if (!render("pl").includes('lang="en"')) problems.push("unknown locale did not fall back to English"); } catch (e) { problems.push("unknown locale crashed: " + e.message); }

if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`render OK for ${catalogs.length} locales: ${catalogs.join(", ")}`);
