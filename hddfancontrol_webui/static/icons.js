"use strict";

// Line icons drawn for this project on a 24 × 24 grid, stroked with the
// current text color. They are built as SVG nodes: no file to fetch, nothing
// inline that the CSP would block.
const ICONS = {
  overview: ["M3.3 18a10 10 0 1 1 17.4 0", "M12 14l4.5-4.5", "M12 14h.01"],
  fan: [
    "M14 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
    { d: "M12 10c-1.2-3.5 0-7 3.5-7 2.8 0 3.3 3.3.8 5.3-1.3 1-2.8 1.5-4.3 1.7", transform: "rotate(0 12 12)" },
    { d: "M12 10c-1.2-3.5 0-7 3.5-7 2.8 0 3.3 3.3.8 5.3-1.3 1-2.8 1.5-4.3 1.7", transform: "rotate(120 12 12)" },
    { d: "M12 10c-1.2-3.5 0-7 3.5-7 2.8 0 3.3 3.3.8 5.3-1.3 1-2.8 1.5-4.3 1.7", transform: "rotate(240 12 12)" },
  ],
  drive: ["M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z", "M3 14h18", "M7 17h.01", "M11 17h.01"],
  thermometer: ["M14 14.8V4.5a2 2 0 0 0-4 0v10.3a4 4 0 1 0 4 0z", "M12 18v-6"],
  smart: ["M3 12h4l2-5 4 10 2-5h6"],
  history: ["M3 3v18h18", "M7 15l4-4 3 3 5-6"],
  settings: ["M4 6h9", "M17 6h3", "M15 4v4", "M4 12h3", "M11 12h9", "M9 10v4", "M4 18h11", "M19 18h1", "M17 16v4"],
  pwmtest: ["M12 21a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "M12 9v4l2.5 2", "M10 2h4"],
  security: ["M6 11h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1z", "M8 11V7a4 4 0 0 1 8 0v4"],
  logs: ["M4 6h16", "M4 10h16", "M4 14h10", "M4 18h7"],
  wizard: ["M4 20L15 9", "M13 7l2 2", "M18 3v4", "M16 5h4", "M20 11v3", "M18.5 12.5h3", "M9 3v2", "M8 4h2"],
  restart: ["M3 12a9 9 0 1 0 2.6-6.4", "M3 4v5h5"],
  warning: ["M12 3.5l9.5 16.5h-19z", "M12 10v4", "M12 17h.01"],
  error: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M15 9l-6 6", "M9 9l6 6"],
  info: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 11v6", "M12 7.5h.01"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  moon: ["M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"],
  logout: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"],
  chip: ["M7 7h10v10H7z", "M10 3v4", "M14 3v4", "M10 17v4", "M14 17v4", "M3 10h4", "M3 14h4", "M17 10h4", "M17 14h4"],
  bell: ["M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z", "M10 21h4"],
  backup: ["M4 7h16v13H4z", "M3 4h18v3H3z", "M10 11h4"],
  download: ["M12 4v11", "M7 10l5 5 5-5", "M5 20h14"],
  upload: ["M12 15V4", "M7 9l5-5 5 5", "M5 20h14"],
  arrow_left: ["M19 12H5", "M11 6l-6 6 6 6"],
  arrow_right: ["M5 12h14", "M13 6l6 6-6 6"],
  trend_up: ["M4 17l6-6 4 4 6-7", "M15 8h5v5"],
  trend_down: ["M4 7l6 6 4-4 6 7", "M15 16h5v-5"],
  chevron: ["M9 6l6 6-6 6"],
  calendar: ["M4 6h16v15H4z", "M4 10h16", "M8 3v4", "M16 3v4", "M8 14h2", "M14 14h2", "M8 17h2"],
  bays: ["M4 3h16v18H4z", "M4 9h16", "M4 15h16", "M7 6h.01", "M7 12h.01", "M7 18h.01", "M12 6h5", "M12 12h5", "M12 18h5"],
  sun: ["M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M12 2v2", "M12 20v2", "M4.9 4.9l1.4 1.4", "M17.7 17.7l1.4 1.4", "M2 12h2", "M20 12h2", "M4.9 19.1l1.4-1.4", "M17.7 6.3l1.4-1.4"],
  contrast: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 3v18", "M12 3a9 9 0 0 1 0 18z"],
};

// The tab of the fans uses the fan.
ICONS.fans = ICONS.fan;

// An <svg> for the named icon, hidden from screen readers (the text next to it says what it is).
function icon(name, extraClass = "") {
  const NS = "http://www.w3.org/2000/svg";
  const node = document.createElementNS(NS, "svg");
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("class", `icon icon-${name} ${extraClass}`.trim());
  node.setAttribute("aria-hidden", "true");
  node.setAttribute("focusable", "false");
  for (const part of ICONS[name] || []) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", typeof part === "string" ? part : part.d);
    if (typeof part !== "string" && part.transform) path.setAttribute("transform", part.transform);
    node.append(path);
  }
  return node;
}

// Put the icon named by data-icon in front of the text of every such element.
function applyIcons(root) {
  for (const node of root.querySelectorAll("[data-icon]")) {
    if (node.querySelector(":scope > svg.icon")) continue;
    node.prepend(icon(node.dataset.icon));
  }
}
