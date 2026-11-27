"use strict";

// Translations: flat keys ("tab.overview") in static/i18n/<lang>.json.
// English is always loaded first, so a missing key in another language
// falls back to English instead of showing the key.
const I18N = { strings: {}, lang: "en", locale: "en" };
const LANGUAGES = ["en", "it", "es", "fr", "de", "pt", "ru", "zh", "hi", "ar"];
// Each language is named in itself, so anyone finds their own.
const LANGUAGE_NAMES = { en: "English", it: "Italiano", es: "Español", fr: "Français", de: "Deutsch", pt: "Português (Brasil)", ru: "Русский", zh: "中文", hi: "हिन्दी", ar: "العربية" };
// Written from right to left: the page turns around.
const RTL_LANGUAGES = ["ar"];
// Dates and numbers: Latin digits everywhere, as the values of the page use them.
const LOCALES = { pt: "pt-BR", zh: "zh-CN", hi: "hi-IN-u-nu-latn", ar: "ar-u-nu-latn" };
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

// Flags drawn as SVG, the same on every system (Windows shows no flag emoji).
// English gets the British flag, Spanish the Spanish one, Portuguese the
// Brazilian one, Hindi the Indian one; Arabic and Russian, spoken in many
// countries, a letter of their own; "automatic" a globe.
const FLAGS = {
  "": { viewBox: "0 0 24 24", parts: [["circle", { cx: 12, cy: 12, r: 9, class: "globe" }], ["path", { d: "M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18", class: "globe" }]] },
  en: {
    viewBox: "0 0 60 30",
    parts: [
      ["rect", { width: 60, height: 30, fill: "#012169" }],
      ["path", { d: "M0,0 L60,30 M60,0 L0,30", stroke: "#fff", "stroke-width": 6 }],
      ["path", { d: "M0,0 L60,30 M60,0 L0,30", stroke: "#C8102E", "stroke-width": 2 }],
      ["path", { d: "M30,0 V30 M0,15 H60", stroke: "#fff", "stroke-width": 10 }],
      ["path", { d: "M30,0 V30 M0,15 H60", stroke: "#C8102E", "stroke-width": 6 }],
    ],
  },
  it: {
    viewBox: "0 0 3 2",
    parts: [
      ["rect", { width: 1, height: 2, fill: "#009246" }],
      ["rect", { x: 1, width: 1, height: 2, fill: "#fff" }],
      ["rect", { x: 2, width: 1, height: 2, fill: "#CE2B37" }],
    ],
  },
  es: {
    viewBox: "0 0 3 2",
    parts: [
      ["rect", { width: 3, height: 2, fill: "#AA151B" }],
      ["rect", { y: 0.5, width: 3, height: 1, fill: "#F1BF00" }],
    ],
  },
  fr: {
    viewBox: "0 0 3 2",
    parts: [
      ["rect", { width: 1, height: 2, fill: "#002395" }],
      ["rect", { x: 1, width: 1, height: 2, fill: "#fff" }],
      ["rect", { x: 2, width: 1, height: 2, fill: "#ED2939" }],
    ],
  },
  de: {
    viewBox: "0 0 5 3",
    parts: [
      ["rect", { width: 5, height: 1, fill: "#000" }],
      ["rect", { y: 1, width: 5, height: 1, fill: "#DD0000" }],
      ["rect", { y: 2, width: 5, height: 1, fill: "#FFCE00" }],
    ],
  },
  pt: {
    viewBox: "0 0 20 14",
    parts: [
      ["rect", { width: 20, height: 14, fill: "#009C3B" }],
      ["path", { d: "M10,1.7 L18.3,7 L10,12.3 L1.7,7Z", fill: "#FFDF00" }],
      ["circle", { cx: 10, cy: 7, r: 3.5, fill: "#002776" }],
    ],
  },
  ru: {
    viewBox: "0 0 30 20",
    parts: [
      ["rect", { width: 30, height: 20, fill: "#3b5998" }],
      ["text", { x: 15, y: 15, "text-anchor": "middle", "font-size": 15, fill: "#fff", text: "Я" }],
    ],
  },
  zh: {
    viewBox: "0 0 30 20",
    parts: [
      ["rect", { width: 30, height: 20, fill: "#EE1C25" }],
      ["path", { d: "M5.00,2.00 L5.67,4.07 L7.85,4.07 L6.09,5.35 L6.76,7.43 L5.00,6.15 L3.24,7.43 L3.91,5.35 L2.15,4.07 L4.33,4.07Z M9.14,2.51 L9.62,1.97 L9.25,1.34 L9.91,1.63 L10.39,1.08 L10.33,1.80 L11.00,2.09 L10.29,2.25 L10.22,2.97 L9.85,2.35Z M11.01,4.14 L11.66,3.82 L11.56,3.10 L12.07,3.62 L12.72,3.30 L12.38,3.95 L12.88,4.47 L12.17,4.34 L11.83,4.99 L11.73,4.27Z M11.04,6.73 L11.76,6.70 L11.96,6.00 L12.21,6.68 L12.94,6.66 L12.37,7.10 L12.62,7.79 L12.01,7.38 L11.44,7.83 L11.64,7.13Z M9.22,8.38 L9.90,8.63 L10.35,8.06 L10.32,8.79 L11.00,9.05 L10.30,9.24 L10.26,9.96 L9.87,9.36 L9.16,9.55 L9.62,8.98Z", fill: "#FFFF00" }],
    ],
  },
  hi: {
    viewBox: "0 0 27 18",
    parts: [
      ["rect", { width: 27, height: 6, fill: "#FF9933" }],
      ["rect", { y: 6, width: 27, height: 6, fill: "#fff" }],
      ["rect", { y: 12, width: 27, height: 6, fill: "#138808" }],
      ["circle", { cx: 13.5, cy: 9, r: 2.4, fill: "none", stroke: "#000080", "stroke-width": 0.5 }],
      ["circle", { cx: 13.5, cy: 9, r: 0.5, fill: "#000080" }],
    ],
  },
  ar: {
    viewBox: "0 0 30 20",
    parts: [
      ["rect", { width: 30, height: 20, fill: "#1f7a4d" }],
      ["text", { x: 15, y: 15, "text-anchor": "middle", "font-size": 15, fill: "#fff", text: "ع" }],
    ],
  },
};

function flag(code) {
  const NS = "http://www.w3.org/2000/svg";
  const spec = FLAGS[code];
  const node = document.createElementNS(NS, "svg");
  node.setAttribute("viewBox", spec.viewBox);
  node.setAttribute("preserveAspectRatio", "xMidYMid slice");
  node.setAttribute("class", code ? "flag" : "flag auto");
  node.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of spec.parts) {
    const part = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "text") part.textContent = value;
      else part.setAttribute(key, String(value));
    }
    node.append(part);
  }
  return node;
}

// Fill every <div class="lang">: a button with the flag of the language in
// use, and a list of the languages; choosing one reloads the page in it.
function setupLanguagePicker() {
  const names = { "": t("lang.auto"), ...LANGUAGE_NAMES };
  for (const box of document.querySelectorAll("div.lang")) {
    const current = savedLanguage();
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lang-button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", `${t("lang.label")}: ${names[current]}`);
    button.title = t("lang.label");
    const name = document.createElement("span");
    name.className = "lang-name";
    name.textContent = current ? current.toUpperCase() : names[""];
    button.append(flag(current), name);
    const list = document.createElement("ul");
    list.className = "lang-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", t("lang.label"));
    list.hidden = true;
    const options = ["", ...LANGUAGES].map((code) => {
      const item = document.createElement("li");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(code === current));
      item.tabIndex = -1;
      item.dataset.code = code;
      item.append(flag(code), document.createTextNode(names[code]));
      item.addEventListener("click", () => choose(code));
      list.append(item);
      return item;
    });
    const choose = (code) => {
      close();
      if (code === current) return;
      saveLanguage(code);
      location.reload();
    };
    const open = () => {
      list.hidden = false;
      button.setAttribute("aria-expanded", "true");
      (options.find((o) => o.dataset.code === current) || options[0]).focus();
    };
    const close = () => {
      list.hidden = true;
      button.setAttribute("aria-expanded", "false");
    };
    button.addEventListener("click", () => (list.hidden ? open() : close()));
    list.addEventListener("keydown", (event) => {
      const index = options.indexOf(document.activeElement);
      if (event.key === "ArrowDown") options[Math.min(options.length - 1, index + 1)].focus();
      else if (event.key === "ArrowUp") options[Math.max(0, index - 1)].focus();
      else if (event.key === "Enter" || event.key === " ") choose(document.activeElement.dataset.code);
      else if (event.key === "Escape") {
        close();
        button.focus();
      } else return;
      event.preventDefault();
    });
    document.addEventListener("click", (event) => {
      if (!box.contains(event.target)) close();
    });
    box.replaceChildren(button, list);
  }
}

// Light, dark, or what the system says ("auto"), kept in this browser. Set
// as soon as this script runs, so the page shows in it from the start.
const THEMES = ["auto", "light", "dark"];
const THEME_KEY = "theme";
const THEME_ICONS = {
  auto: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 3v18", "M12 7h4", "M12 11h6", "M12 15h5"],
  light: ["M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M12 2v2", "M12 20v2", "M4.9 4.9l1.4 1.4", "M17.7 17.7l1.4 1.4", "M2 12h2", "M20 12h2", "M4.9 19.1l1.4-1.4", "M17.7 6.3l1.4-1.4"],
  dark: ["M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"],
};

function savedTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    return THEMES.includes(theme) ? theme : "auto";
  } catch {
    return "auto";
  }
}

function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

applyTheme(savedTheme());

// A button before every language picker that goes auto → light → dark.
function setupThemeToggle() {
  const NS = "http://www.w3.org/2000/svg";
  for (const box of document.querySelectorAll("div.lang")) {
    if (box.previousElementSibling && box.previousElementSibling.classList.contains("theme-button")) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "theme-button";
    const show = (theme) => {
      const node = document.createElementNS(NS, "svg");
      node.setAttribute("viewBox", "0 0 24 24");
      node.setAttribute("aria-hidden", "true");
      for (const d of THEME_ICONS[theme]) {
        const path = document.createElementNS(NS, "path");
        path.setAttribute("d", d);
        node.append(path);
      }
      button.replaceChildren(node);
      const text = t(`theme.${theme}`);
      button.title = text;
      button.setAttribute("aria-label", text);
    };
    show(savedTheme());
    button.addEventListener("click", () => {
      const next = THEMES[(THEMES.indexOf(savedTheme()) + 1) % THEMES.length];
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        // Nothing to keep it in: it lasts for this page.
      }
      applyTheme(next);
      for (const other of document.querySelectorAll(".theme-button")) other.dispatchEvent(new CustomEvent("theme", { detail: next }));
    });
    button.addEventListener("theme", (event) => show(event.detail));
    box.before(button);
  }
}

async function loadI18n() {
  const wanted = (savedLanguage() || navigator.language || "en").slice(0, 2).toLowerCase();
  const lang = LANGUAGES.includes(wanted) ? wanted : "en";
  const load = async (code) => (await fetch(`/static/i18n/${code}.json`)).json();
  const en = await load("en");
  I18N.strings = lang === "en" ? en : Object.assign({}, en, await load(lang));
  I18N.lang = lang;
  I18N.locale = LOCALES[lang] || lang;
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL_LANGUAGES.includes(lang) ? "rtl" : "ltr";
  applyI18n(document);
  setupLanguagePicker();
  setupThemeToggle();
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
