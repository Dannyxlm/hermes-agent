// Unit checks for the bundle's i18n runtime: locale resolution, plural
// selection, number and duration formatting, and section-name lookup.
//
//   node tests/js/runtime_check.mjs
//
// Loads the real bundle into a VM with a stub SDK and reads the helpers it
// exposes on window.__DUTY_BOARD_I18N__ (a test-only hook, no behaviour).
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../dashboard/dist/index.js", import.meta.url), "utf8");

function load(intlOverride) {
  const store = {};
  const window = {
    __HERMES_PLUGIN_SDK__: { React: { createElement: () => null }, hooks: {}, fetchJSON: async () => ({}) },
    __HERMES_PLUGINS__: { register: () => {} },
    localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } },
  };
  const context = { window, navigator: { language: "en" }, console, Intl: intlOverride || Intl, document: {} };
  vm.createContext(context);
  vm.runInContext(source, context);
  return window.__DUTY_BOARD_I18N__;
}

const failures = [];
const eq = (label, actual, expected) => { if (actual !== expected) failures.push(`${label}\n  actual:   ${actual}\n  expected: ${expected}`); };

const i = load();
if (!i) {
  console.error("bundle does not expose window.__DUTY_BOARD_I18N__");
  process.exit(1);
}

// Locale resolution: only real catalogs, never inherited object properties.
for (const bogus of ["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf", "xx", ""]) {
  eq(`localeName(${JSON.stringify(bogus)})`, i.localeName(bogus), "en");
}
eq("localeName(pt-BR)", i.localeName("pt-BR"), "pt");
eq("localeName(zh-TW)", i.localeName("zh-TW"), "zh-hant");
eq("localeName(zh_CN)", i.localeName("zh_CN"), "zh");
eq("localeName(AR)", i.localeName("AR"), "ar");
// An unknown locale must still format without throwing.
eq("tr(constructor) formats", i.tr(i.localeName("constructor"), "activity.call", { n: 1 }), i.tr("en", "activity.call", { n: 1 }));

// Plural selection must follow each language's CLDR rules, not just "other".
// Each row: locale, key, count, exact expected text from the shipped catalog.
const plural = (locale, key, count) => i.tr(locale, key, { count });
const cat = (locale) => JSON.parse(fs.readFileSync(new URL(`../../dashboard/i18n/${locale}.json`, import.meta.url), "utf8"));
const form = (locale, key, category, count) => {
  let v = cat(locale);
  for (const part of key.split(".")) v = v[part];
  return v[category].replace("{count}", String(count));
};
for (const [locale, key, count, category] of [
  ["en", "cronPaused", 1, "one"], ["en", "cronPaused", 2, "other"],
  ["ru", "staleGoals", 1, "one"], ["ru", "staleGoals", 3, "few"], ["ru", "staleGoals", 5, "many"], ["ru", "staleGoals", 21, "one"],
  ["uk", "cronPaused", 2, "few"], ["uk", "cronPaused", 11, "many"],
  ["ar", "cronPaused", 1, "one"], ["ar", "cronPaused", 2, "two"], ["ar", "cronPaused", 3, "few"], ["ar", "cronPaused", 11, "many"], ["ar", "cronPaused", 0, "zero"],
  ["ga", "cronPaused", 2, "two"], ["ga", "cronPaused", 7, "many"],
  ["fr", "cronPaused", 0, "one"], ["fr", "cronPaused", 2, "other"],
]) {
  eq(`${locale} ${key} count=${count} (${category})`, plural(locale, key, count), form(locale, key, category, count));
}

// Numbers are localized after plural selection; Arabic keeps Latin digits.
eq("en 1234 grouped", i.num(1234, "en"), "1,234");
eq("de 1234 grouped", i.num(1234, "de"), "1.234");
eq("ar keeps latin digits", /[0-9]/.test(i.num(42, "ar")) && !/[٠-٩]/.test(i.num(42, "ar")), true);

// Durations: native DurationFormat and the fallback must both produce text with the number in it.
// Intl's members are non-enumerable, so copy them by name, minus DurationFormat.
const noDuration = {};
for (const name of Object.getOwnPropertyNames(Intl)) if (name !== "DurationFormat") noDuration[name] = Intl[name];
const fallback = load(noDuration);
for (const locale of ["en", "ru", "ar", "ja", "ga"]) {
  for (const sec of [0, 45, 300, 3725, 90061]) {
    for (const [name, rt] of [["native", i], ["fallback", fallback]]) {
      const text = rt.dur(sec, locale);
      if (!text || /undefined|NaN|\{/.test(text)) failures.push(`${name} dur(${sec}, ${locale}) = ${JSON.stringify(text)}`);
    }
  }
}

// Section names: an issue for a section whose name is also an object key (gateway) must use the section label.
for (const locale of ["en", "zh", "de", "ar"]) {
  const text = i.issueText(locale, { section: "gateway", code: "collectorFailed", detail: "AttributeError" });
  const label = cat(locale).section.gateway;
  if (!text.includes(label)) failures.push(`${locale} gateway issue does not use section.gateway label "${label}": ${text}`);
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("i18n runtime OK");
