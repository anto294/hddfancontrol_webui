"use strict";

// Translations: flat keys ("tab.overview") in static/i18n/<lang>.json.
// English is always loaded first, so a missing key in another language
// falls back to English instead of showing the key.
const I18N = { strings: {}, lang: "en" };
const LANGUAGES = ["en", "it"];

async function loadI18n() {
  const wanted = (navigator.language || "en").slice(0, 2).toLowerCase();
  const lang = LANGUAGES.includes(wanted) ? wanted : "en";
  const load = async (code) => (await fetch(`/static/i18n/${code}.json`)).json();
  const en = await load("en");
  I18N.strings = lang === "en" ? en : Object.assign({}, en, await load(lang));
  I18N.lang = lang;
  document.documentElement.lang = lang;
  applyI18n(document);
}

function t(key, params = {}) {
  let text = I18N.strings[key] ?? key;
  for (const [name, value] of Object.entries(params)) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}

function applyI18n(root) {
  for (const node of root.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
}
