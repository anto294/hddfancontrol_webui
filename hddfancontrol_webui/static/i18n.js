"use strict";

// Translations: flat keys ("tab.overview") in static/i18n/<lang>.json.
// English is always loaded first, so a missing key in another language
// falls back to English instead of showing the key.
const I18N = { strings: {}, lang: "en" };
const LANGUAGES = ["en", "it"];
// Each language is named in itself, so anyone finds their own.
const LANGUAGE_NAMES = { en: "English", it: "Italiano" };
const LANGUAGE_KEY = "lang";

// The language chosen in this browser, or "" to follow the browser. Storage
// can be missing (private windows); then the choice lasts for the page only.
function savedLanguage() {
  try {
    return localStorage.getItem(LANGUAGE_KEY) || "";
  } catch {
    return "";
  }
}

function saveLanguage(code) {
  try {
    if (code) localStorage.setItem(LANGUAGE_KEY, code);
    else localStorage.removeItem(LANGUAGE_KEY);
  } catch {
    // Nothing to keep it in.
  }
}

// Fill every <select class="lang"> of the page; a change reloads the page in that language.
function setupLanguagePicker() {
  for (const select of document.querySelectorAll("select.lang")) {
    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = t("lang.auto");
    select.replaceChildren(auto);
    for (const code of LANGUAGES) {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = LANGUAGE_NAMES[code];
      select.append(option);
    }
    select.value = savedLanguage();
    select.setAttribute("aria-label", t("lang.label"));
    select.addEventListener("change", () => {
      saveLanguage(select.value);
      location.reload();
    });
  }
}

async function loadI18n() {
  const wanted = (savedLanguage() || navigator.language || "en").slice(0, 2).toLowerCase();
  const lang = LANGUAGES.includes(wanted) ? wanted : "en";
  const load = async (code) => (await fetch(`/static/i18n/${code}.json`)).json();
  const en = await load("en");
  I18N.strings = lang === "en" ? en : Object.assign({}, en, await load(lang));
  I18N.lang = lang;
  document.documentElement.lang = lang;
  applyI18n(document);
  setupLanguagePicker();
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
