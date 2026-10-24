// Validate the locale catalogs in dashboard/i18n/.
//
//   node tests/js/i18n_check.mjs             all locales, and every supported locale must exist
//   node tests/js/i18n_check.mjs --only de   one locale (handy while translating)
//
// Checks, against en.json (the source of truth):
//   - same keys, nothing missing and nothing extra;
//   - every string uses exactly the same {placeholders};
//   - plural messages carry exactly the CLDR plural categories of their locale
//     (Intl.PluralRules), "other" carries every placeholder, and the other
//     forms use no placeholder English does not have;
//   - no empty strings, and no leftover English in a non-English catalog
//     beyond an allow-list of brand names and identical-by-design strings.
import fs from "node:fs";
import { pathToFileURL } from "node:url";

// Every locale the Hermes dashboard ships (web/src/i18n/types.ts).
export const SUPPORTED = ["en", "zh", "zh-hant", "ja", "de", "es", "fr", "tr", "uk", "af", "ko", "it", "ga", "pt", "ru", "hu", "ar"];
const PLURAL_KEYS = new Set(["zero", "one", "two", "few", "many", "other"]);
// Strings that may legitimately equal the English text.
const SAME_AS_EN_OK = new Set(["appTitle", "web"]);

// I18N_DIR lets translators validate a scratch directory that also holds en.json.
const dir = process.env.I18N_DIR ? pathToFileURL(process.env.I18N_DIR.replace(/\/?$/, "/")) : new URL("../../dashboard/i18n/", import.meta.url);
const read = (locale) => JSON.parse(fs.readFileSync(new URL(`${locale}.json`, dir), "utf8"));
const en = read("en");

const isPlural = (value) => value && typeof value === "object" && Object.keys(value).length > 0 && Object.keys(value).every((k) => PLURAL_KEYS.has(k));
const placeholders = (text) => new Set([...String(text).matchAll(/\{(\w+)\}/g)].map((m) => m[1]));
const setEq = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));
const fmt = (s) => "{" + [...s].sort().join(", ") + "}";

function walk(ref, value, path, out) {
  for (const key of Object.keys(ref)) {
    const p = path ? `${path}.${key}` : key;
    if (!(key in value)) { out.push(`missing key ${p}`); continue; }
    const r = ref[key], v = value[key];
    if (isPlural(r)) out.push(["plural", p, r, v]);
    else if (r && typeof r === "object") {
      if (!v || typeof v !== "object") out.push(`${p} must be an object`);
      else walk(r, v, p, out);
    } else out.push(["string", p, r, v]);
  }
  for (const key of Object.keys(value)) if (!(key in ref)) out.push(`extra key ${path ? path + "." : ""}${key}`);
}

export function check(locale) {
  const errors = [];
  let data;
  try { data = read(locale); } catch (e) { return [`cannot read ${locale}.json: ${e.message}`]; }
  const items = [];
  walk(en, data, "", items);
  const categories = new Set(new Intl.PluralRules(locale).resolvedOptions().pluralCategories);
  let sameAsEn = 0, strings = 0;
  for (const item of items) {
    if (typeof item === "string") { errors.push(item); continue; }
    const [kind, path, ref, value] = item;
    if (kind === "string") {
      strings++;
      if (typeof value !== "string" || !value.trim()) { errors.push(`${path}: empty or not a string`); continue; }
      const want = placeholders(ref), got = placeholders(value);
      if (!setEq(want, got)) errors.push(`${path}: placeholders ${fmt(got)} != en ${fmt(want)}`);
      if (locale !== "en" && value === ref && !SAME_AS_EN_OK.has(path) && !path.startsWith("platform.") && /[a-z]{3}/i.test(ref)) sameAsEn++;
    } else {
      strings++;
      if (!isPlural(value)) { errors.push(`${path}: must be a plural object with keys ${fmt(categories)}`); continue; }
      const got = new Set(Object.keys(value));
      if (!setEq(got, categories)) errors.push(`${path}: plural categories ${fmt(got)} != ${locale} needs ${fmt(categories)}`);
      const want = new Set(Object.values(ref).flatMap((t) => [...placeholders(t)]));
      for (const [cat, text] of Object.entries(value)) {
        if (typeof text !== "string" || !text.trim()) { errors.push(`${path}.${cat}: empty`); continue; }
        const ph = placeholders(text);
        for (const name of ph) if (!want.has(name)) errors.push(`${path}.${cat}: unknown placeholder {${name}}`);
        if (cat === "other" && !setEq(ph, want)) errors.push(`${path}.other: placeholders ${fmt(ph)} != en ${fmt(want)}`);
      }
    }
  }
  if (locale !== "en" && sameAsEn > 3) errors.push(`${sameAsEn} strings are still identical to English`);
  return errors.length ? errors : [`ok (${strings} messages)`];
}

const only = process.argv.indexOf("--only");
const locales = only > 0 ? [process.argv[only + 1]] : SUPPORTED;
let failed = false;
if (only < 0) {
  const present = fs.readdirSync(dir).filter((n) => n.endsWith(".json")).map((n) => n.slice(0, -5));
  for (const locale of SUPPORTED) if (!present.includes(locale)) { console.error(`missing catalog ${locale}.json`); failed = true; }
  for (const locale of present) if (!SUPPORTED.includes(locale)) { console.error(`unknown catalog ${locale}.json (not a Hermes dashboard locale)`); failed = true; }
}
if (only > 0 && (!locales[0] || !SUPPORTED.includes(locales[0]))) {
  console.error(`--only needs a Hermes dashboard locale (${SUPPORTED.join(", ")}), got ${JSON.stringify(locales[0] || "")}`);
  process.exit(1);
}
for (const locale of locales) {
  if (!fs.existsSync(new URL(`${locale}.json`, dir))) { console.error(`${locale}: catalog ${locale}.json not found`); failed = true; continue; }
  const result = check(locale);
  const ok = result.length === 1 && result[0].startsWith("ok");
  if (!ok) failed = true;
  console[ok ? "log" : "error"](`${locale}: ${result.join("\n  ")}`);
}
if (failed) process.exit(1);
console.log(`i18n catalogs OK: ${locales.length} locale(s)`);
