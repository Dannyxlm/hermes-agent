#!/usr/bin/env node
// Inline dashboard/i18n/*.json into dashboard/dist/index.js between the i18n markers.
//
//   node tools/build_i18n.mjs          rewrite the bundle
//   node tools/build_i18n.mjs --check  exit 1 if the bundle is out of date (CI)
//
// The JSON files are the source of truth. The bundle carries a copy because the
// dashboard loads one plain script with no build step on the user's machine.
import fs from "node:fs";

const root = new URL("../", import.meta.url);
const dir = new URL("dashboard/i18n/", root);
const bundlePath = new URL("dashboard/dist/index.js", root);
const START = "/* i18n:start */";
const END = "/* i18n:end */";

const files = fs.readdirSync(dir).filter((name) => name.endsWith(".json")).sort();
const messages = {};
for (const name of ["en.json", ...files.filter((n) => n !== "en.json")]) {
  messages[name.slice(0, -5)] = JSON.parse(fs.readFileSync(new URL(name, dir), "utf8"));
}

const body = "  const MESSAGES = " + JSON.stringify(messages) + ";\n  ";
const source = fs.readFileSync(bundlePath, "utf8");
const start = source.indexOf(START);
const end = source.indexOf(END);
if (start < 0 || end < start) {
  console.error("i18n markers not found in dashboard/dist/index.js");
  process.exit(2);
}
const next = source.slice(0, start + START.length) + "\n" + body + source.slice(end);

if (process.argv.includes("--check")) {
  if (next !== source) {
    console.error("dashboard/dist/index.js is out of date with dashboard/i18n/*.json; run: node tools/build_i18n.mjs");
    process.exit(1);
  }
  console.log(`bundle i18n up to date (${Object.keys(messages).length} locales)`);
} else {
  fs.writeFileSync(bundlePath, next);
  console.log(`inlined ${Object.keys(messages).length} locales into dashboard/dist/index.js`);
}
