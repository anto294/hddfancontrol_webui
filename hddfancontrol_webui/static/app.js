"use strict";

// Every value from the server goes into the page with textContent or as an
// attribute, never as HTML: drive models and log lines are not trusted.

const state = {
  status: null,
  hardware: { drives: [], hwmons: [] },
  settings: null,
  settingsSource: "",
  labels: {},
  tab: "overview",
  logSeq: 0,
  logLines: [],
  dragging: new Set(),
  showAcked: false,
  pending: new Map(),
  // Kernel name of a drive (sda) -> change of its temperature over TREND_SECONDS.
  trend: new Map(),
  // The last SMART data, for the hints of the settings (recommended maximum).
  smartData: null,
  // Where each drive sits in the case (drives.js).
  bays: null,
};

const POLL_STATUS_MS = 3000;
const POLL_HARDWARE_FAST_MS = 2000;
const POLL_HARDWARE_SLOW_MS = 15000;
const POLL_LOGS_MS = 2000;
const POLL_HISTORY_MS = 60000;
// The arrow next to a drive compares its temperature with this long ago, and
// shows from this many degrees of change.
const TREND_SECONDS = 600;
const TREND_MIN_DELTA = 1;
const LOG_MAX_LINES = 2000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api(method, url, body) {
  const options = { method, headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (response.status === 401 && !url.startsWith("/api/auth")) {
    location.href = "/login";
    throw new Error("unauthorized");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || String(response.status));
    error.data = data;
    throw error;
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function row(...cells) {
  return el("tr", {}, ...cells.map((cell) => el("td", {}, cell)));
}

function headRow(...cells) {
  return el("tr", {}, ...cells.map((cell) => el("th", { text: cell })));
}

function $(id) {
  return document.getElementById(id);
}

function fmtTemp(value) {
  return value === null || value === undefined ? "–" : `${Number(value).toFixed(1)} °C`;
}

function fmtPct(pwm) {
  return pwm === null || pwm === undefined ? "–" : `${Math.round((pwm * 100) / 255)} %`;
}

function fmtRpm(rpm) {
  return rpm === null || rpm === undefined ? "–" : `${rpm} RPM`;
}

function fmtBytes(bytes) {
  if (!bytes) return "–";
  const units = ["B", "kB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

function fmtTime(value) {
  if (!value) return "–";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return date.toLocaleString(I18N.lang);
}

function shortPath(path) {
  return path.split("/").pop();
}

// The name given to a fan, or "nct6798 · pwm2" for /sys/class/hwmon/hwmon3/pwm2.
function pwmLabel(path) {
  if (state.labels[path]) return state.labels[path];
  for (const device of state.hardware.hwmons) {
    if (device.pwms.some((pwm) => pwm.path === path)) {
      return `${device.name || device.hwmon} · ${shortPath(path)}`;
    }
  }
  return path;
}

// The name given to a drive, looked up by any of its stable names.
function driveLabelById(id) {
  if (state.labels[id]) return state.labels[id];
  const drive = state.hardware.drives.find((d) => d.ids.includes(id));
  const named = drive && drive.ids.find((i) => state.labels[i]);
  return named ? state.labels[named] : "";
}

// "WD40EFRX-68N32N0" for "WDC WD40EFRX-68N32N0": the prefix only repeats the brand.
function shortModel(model) {
  return (model || "").replace(/^(WDC|TOSHIBA|HGST|Hitachi|SAMSUNG|ATA)[ _]+/i, "").trim();
}

// What a drive of the temperature log is called on the overview: the name
// given to it, or its model; then the kernel name and the size, smaller.
function driveTitle(logName) {
  const [kernel, ...rest] = logName.split(" ");
  const drive = state.hardware.drives.find((d) => d.name === kernel);
  const label = drive ? driveLabelById(drive.ids[0]) : "";
  const model = shortModel(drive ? drive.model : rest.join(" "));
  const sub = [label ? model : "", kernel, drive && drive.size_bytes ? fmtBytes(drive.size_bytes) : ""].filter(Boolean).join(" · ");
  return { name: label || model || kernel, sub };
}

async function saveLabel(key, label) {
  try {
    state.labels = await api("PUT", "/api/labels", { key, label });
  } catch (error) {
    alertBanner(errorText(error));
  }
  renderOverview();
}

function labelInput(key, current) {
  const input = el("input", {
    type: "text",
    class: "label-input",
    value: current || "",
    maxlength: 40,
    placeholder: t("labels.placeholder"),
    "aria-label": t("labels.placeholder"),
  });
  input.addEventListener("change", () => saveLabel(key, input.value));
  return input;
}

function allPwms() {
  return state.hardware.hwmons.flatMap((device) => device.pwms.map((pwm) => ({ ...pwm, chip: device.name || device.hwmon })));
}

function errorText(error) {
  if (error && error.data && error.data.error) return t(`error.${error.data.error}`);
  return t("error.network");
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function showTab(name) {
  state.tab = name;
  for (const button of document.querySelectorAll("[data-tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
    // On a phone the tab bar scrolls: keep the tab chosen in sight.
    if (button.dataset.tab === name) button.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  for (const section of document.querySelectorAll(".tab")) {
    section.hidden = section.id !== `tab-${name}`;
  }
  try {
    localStorage.setItem("tab", name);
  } catch {
    // Storage may be blocked; the tab is only a convenience.
  }
  if (name === "history") loadHistory();
  if (name === "smart") loadSmart();
  if (name === "settings") renderSettings();
  if (name === "logs") renderLogs();
  if (name === "security") loadAuth();
  if (name === "fans" || name === "overview") pollHardware();
}

// ---------------------------------------------------------------------------
// Status, banners, overview
// ---------------------------------------------------------------------------

function banner(kind, text, ...extra) {
  return el(
    "div",
    { class: `banner ${kind}`, role: kind === "error" ? "alert" : "status" },
    icon(kind),
    el("div", { class: "banner-body" }, el("p", { text }), ...extra),
  );
}

function renderBanners() {
  const status = state.status;
  const box = $("banners");
  box.replaceChildren();
  if (!status) return;
  // Warnings taken note of are hidden, unless the user asks to see them.
  let hidden = 0;
  const shown = (acked) => {
    if (acked && !state.showAcked) hidden += 1;
    return !acked || state.showAcked;
  };
  if (!status.auth.enabled) {
    const acked = status.acknowledged.includes("banner:no_auth");
    if (shown(acked)) {
      box.append(
        banner(
          "warning",
          t("banner.no_auth"),
          el("div", { class: "banner-actions" }, el("button", { class: "link", text: t("tab.security"), onclick: () => showTab("security") }), ackButton("banner:no_auth", acked)),
        ),
      );
    }
  }
  const sensors = status.sensors;
  if (sensors.state === "running") box.append(banner("info", t("banner.scanning")));
  for (const module of sensors.modules.filter((m) => m.status === "failed")) {
    box.append(
      banner(
        "warning",
        t("banner.module_failed", { module: module.module, error: module.error }),
        el("p", { class: "small", text: t("banner.truenas_hint", { module: module.module }) }),
        el("button", { class: "secondary", text: t("action.rescan"), onclick: rescanSensors }),
      ),
    );
  }
  if (sensors.state === "done" && state.hardware.hwmons.length > 0 && allPwms().length === 0) {
    box.append(banner("warning", t("banner.no_pwm")));
  }
  if (status.read_only) box.append(banner("error", t("banner.read_only")));
  if (status.missing_pwms.length) box.append(banner("error", t("banner.missing_pwms", { paths: status.missing_pwms.map(pwmLabel).join(", ") })));
  if (status.settings_error) box.append(banner("error", t("banner.settings_error")));
  // Safety and missing fans have their own banners.
  for (const alert of status.alerts.filter((a) => a.kind !== "safety" && a.kind !== "fans_missing")) {
    if (!shown(alert.acknowledged)) continue;
    box.append(banner(["smart_warning", "smart_error", "fan_wear"].includes(alert.kind) ? "warning" : "error", alertText(alert), el("div", { class: "banner-actions" }, ackButton(alert.key, alert.acknowledged))));
  }
  const anyAcked = status.alerts.some((a) => a.acknowledged) || status.acknowledged.length > 0;
  if (hidden || (state.showAcked && anyAcked)) {
    box.append(
      el(
        "p",
        { class: "muted small hidden-alerts" },
        hidden ? `${hidden === 1 ? t("alert.hidden_one") : t("alert.hidden", { count: hidden })} ` : "",
        el("button", {
          class: "link",
          text: state.showAcked ? t("alert.hide_hidden") : t("alert.show_hidden"),
          onclick: () => {
            state.showAcked = !state.showAcked;
            renderBanners();
          },
        }),
      ),
    );
  }
  if (status.mode === "waiting" && state.tab !== "setup") {
    box.append(banner("info", t("banner.waiting"), el("button", { class: "link", text: t("wizard.open"), onclick: openWizard })));
  }
  if (status.fans.auto.failsafe) box.append(banner("error", t("banner.failsafe")));
  if (status.fans.error) box.append(banner("error", t("banner.control_error", { error: status.fans.error })));
  else if (status.fans.safety) box.append(banner("error", status.fans.safety_reason === "stale" ? t("banner.safety_stale") : t("banner.safety")));
}

// The minimum and maximum of a drive: its own (Limits per drive), or the common ones.
// kernel is its kernel name (sda); without one, the common limits.
function limitsOf(kernel) {
  const settings = state.settings;
  if (!settings) return { min: 30, max: 50 };
  const drive = kernel ? state.hardware.drives.find((d) => d.name === kernel) : null;
  const paths = drive ? [...drive.ids, `/dev/${drive.name}`] : [];
  const id = Object.keys(settings.drive_limits || {}).find((key) => paths.includes(key));
  const own = id ? settings.drive_limits[id] : {};
  return { min: own.min ?? settings.drive_temp_min, max: own.max ?? settings.drive_temp_max };
}

// cool: below the minimum; mild and warm: the two halves of the range; hot: at the maximum.
function tempLevel(value, kernel) {
  const { min: low, max: high } = limitsOf(kernel);
  if (value < low) return "cool";
  if (value >= high) return "hot";
  return value < (low + high) / 2 ? "mild" : "warm";
}

// A bar as an SVG: its width is an attribute, so the CSP (no style attributes) allows it.
function bar(fraction, level) {
  const clamped = Math.max(0, Math.min(1, fraction));
  const node = svg("svg", { class: `bar ${level}`, viewBox: "0 0 100 8", preserveAspectRatio: "none", "aria-hidden": "true" });
  node.append(svg("rect", { class: "track", x: 0, y: 0, width: 100, height: 8, rx: 4 }));
  if (clamped > 0) node.append(svg("rect", { class: "fill", x: 0, y: 0, width: (clamped * 100).toFixed(1), height: 8, rx: 4 }));
  return node;
}

// The fan icon turns faster with the speed; it stands still when the fan does.
function spinClass(pwm) {
  if (pwm.rpm === 0 || pwm.value === 0) return "still";
  if (pwm.value === null) return "still";
  const pct = (pwm.value * 100) / 255;
  return pct < 40 ? "spin-slow" : pct < 75 ? "spin-mid" : "spin-fast";
}

function setStat(id, value, level = "") {
  const node = $(id);
  node.querySelector(".stat-value").textContent = value;
  node.className = `stat ${level}`.trim();
}

// The text of an alert; SMART warning codes become sentences.
function alertText(alert) {
  const params = { ...alert.params };
  if (Array.isArray(params.warnings)) params.problems = params.warnings.map((code) => t(`smart.warn.${code}`)).join(" ");
  return t(`alert.${alert.kind}`, params);
}

function ackButton(key, acked) {
  const button = el("button", {
    class: "link ack",
    text: acked ? t("alert.unacknowledge") : t("alert.acknowledge"),
    onclick: async () => {
      try {
        await api("PUT", "/api/alerts/ack", { key, acknowledged: !acked });
      } catch (error) {
        alertBanner(errorText(error));
      }
      await pollStatus();
    },
  });
  button.prepend(icon(acked ? "warning" : "check"));
  return button;
}

function renderOverview() {
  const status = state.status;
  if (!status) return;

  const temps = $("drive-temps");
  temps.replaceChildren();
  const values = [];
  let asleep = 0;
  if (status.latest) {
    $("latest-time").textContent = t("overview.measured", { time: fmtTime(status.latest.time) });
    const low = state.settings ? state.settings.drive_temp_min : 30;
    const high = state.settings ? state.settings.drive_temp_max : 50;
    // The bar runs from 10 degrees under the minimum to 5 over the maximum.
    const from = low - 10;
    const to = high + 5;
    for (const [name, value] of Object.entries(status.latest.temps)) {
      const title = driveTitle(name);
      const label = el("span", { class: "meter-name" }, el("span", { class: "meter-title", text: title.name, title: title.name }), el("small", { class: "muted", text: title.sub }));
      if (value === null) {
        asleep += 1;
        temps.append(el("div", { class: "meter drive asleep" }, icon("moon"), label, bar(0, "cool"), el("span"), el("span", { class: "meter-value muted", text: t("state.asleep") })));
        continue;
      }
      values.push(value);
      const level = tempLevel(value, name.split(" ")[0]);
      temps.append(el("div", { class: `meter drive ${level}` }, icon("drive"), label, bar((value - from) / (to - from), level), trendMark(name.split(" ")[0]), el("span", { class: "meter-value", text: fmtTemp(value) })));
    }
  } else {
    $("latest-time").textContent = t("overview.no_temps");
  }
  if (values.length) {
    const hottest = Math.max(...values);
    // The tile takes the worst level of any drive, each against its own limits.
    const order = ["cool", "mild", "warm", "hot"];
    const worst = Object.entries(status.latest.temps)
      .filter(([, value]) => value !== null)
      .map(([name, value]) => tempLevel(value, name.split(" ")[0]))
      .reduce((a, b) => (order.indexOf(b) > order.indexOf(a) ? b : a), "cool");
    setStat("stat-hottest", fmtTemp(hottest), worst);
  } else {
    setStat("stat-hottest", "–");
  }
  const total = values.length + asleep;
  setStat("stat-drives", total ? `${values.length} / ${total}` : "–");
  $("stat-drives").title = asleep ? t("overview.asleep_count", { count: asleep }) : "";

  renderBays();

  const fans = $("fan-summary");
  const configured = new Set((state.settings ? state.settings.pwms : []).map((pwm) => pwm.path));
  // The configured fans, or every PWM output when none of them is found.
  const matching = allPwms().filter((pwm) => configured.has(pwm.path));
  const shown = matching.length ? matching : allPwms();
  // Lines are updated in place: building them again would restart the turning icons.
  const lines = new Map([...fans.querySelectorAll(".meter[data-path]")].map((line) => [line.dataset.path, line]));
  const order = [];
  for (const pwm of shown) {
    let line = lines.get(pwm.path);
    if (!line) {
      line = el(
        "div",
        { class: "meter fan", "data-path": pwm.path },
        icon("fan"),
        el("span", { class: "meter-name" }),
        bar(0, "fan"),
        el("span", { class: "meter-value" }, el("span", { class: "pct" }), el("small", { class: "muted rpm" })),
      );
    }
    const fan = line.querySelector(".icon");
    const spin = `icon icon-fan ${spinClass(pwm)}`;
    if (fan.getAttribute("class") !== spin) fan.setAttribute("class", spin);
    line.querySelector(".meter-name").textContent = pwmLabel(pwm.path);
    line.querySelector(".bar").replaceWith(bar(pwm.value === null ? 0 : pwm.value / 255, "fan"));
    line.querySelector(".pct").textContent = fmtPct(pwm.value);
    line.querySelector(".rpm").textContent = fmtRpm(pwm.rpm);
    order.push(line);
  }
  fans.replaceChildren(...order);
  const known = shown.filter((pwm) => pwm.value !== null);
  setStat("stat-fans", known.length ? fmtPct(known.reduce((sum, pwm) => sum + pwm.value, 0) / known.length) : "–");

  const control = $("control");
  control.replaceChildren();
  control.append(row(t("overview.mode"), t(`mode.${status.mode}`)));
  control.append(row(t("settings.control_mode"), t(`mode.${status.control_mode}`)));
  if ((status.fans.mode === "auto" || status.fans.mode === "curve") && status.fans.auto.speed !== null) {
    control.append(row(t("overview.hottest"), fmtTemp(status.fans.auto.hottest)));
    // With fan groups each fan has its own speed.
    for (const [path, speed] of Object.entries(status.fans.auto.speeds || {})) {
      control.append(row(t("overview.fan_speed", { fan: pwmLabel(path) }), `${Math.round(speed * 100)} %`));
    }
  }
  for (const [name, daemon] of Object.entries(status.daemons)) {
    const text = t(`daemon.${daemon.state}`) + (daemon.restarts ? ` · ${t("overview.restarts", { count: daemon.restarts })}` : "");
    control.append(row(name, text));
  }
}

// An arrow when a drive got warmer or cooler by TREND_MIN_DELTA in TREND_SECONDS.
function trendMark(kernel) {
  const delta = state.trend.get(kernel);
  if (delta === undefined || Math.abs(delta) < TREND_MIN_DELTA) return el("span", { class: "trend" });
  const up = delta > 0;
  const text = t(up ? "overview.trend_up" : "overview.trend_down", { delta: Math.abs(delta).toFixed(1), minutes: TREND_SECONDS / 60 });
  return el("span", { class: `trend ${up ? "up" : "down"}`, title: text, "aria-label": text }, icon(up ? "trend_up" : "trend_down"));
}

// How much each drive changed in TREND_SECONDS, from the history of the server.
async function loadTrend() {
  try {
    const data = await api("GET", `/api/history?hours=${(2 * TREND_SECONDS) / 3600}`);
    const trend = new Map();
    for (const series of data.series.filter((s) => s.kind === "temp" && s.points.length > 1)) {
      const drive = state.hardware.drives.find((d) => d.ids.includes(series.name));
      const kernel = drive ? drive.name : series.name.split(" ")[0];
      const [lastTs, lastValue] = series.points[series.points.length - 1];
      // The newest point at least TREND_SECONDS older, or the oldest one there is.
      const before = [...series.points].reverse().find(([ts]) => ts <= lastTs - TREND_SECONDS) || series.points[0];
      trend.set(kernel, lastValue - before[1]);
    }
    state.trend = trend;
    renderOverview();
  } catch {
    // Only the arrows are missing; the next round tries again.
  }
}

function renderHeader() {
  const status = state.status;
  if (!status) return;
  const pill = $("mode");
  pill.textContent = t(`mode.${status.mode}`);
  pill.dataset.mode = status.mode;
  // Releases are numbers (0.3.0); "dev" and "edge-<commit>" are shown as they are.
  $("version").textContent = /^\d/.test(status.version) ? `v${status.version}` : status.version;
  $("logout").hidden = !status.auth.enabled;
}

async function pollStatus() {
  try {
    state.status = await api("GET", "/api/status");
    renderHeader();
    renderBanners();
    renderOverview();
    renderFansState();
    renderPwmTest();
    wizardOnStatus();
  } catch (error) {
    if (error.message !== "unauthorized") $("banners").replaceChildren(banner("error", t("error.network")));
  }
}

async function pollHardware() {
  try {
    state.hardware = await api("GET", "/api/hardware");
    renderOverview();
    renderFans();
    renderPwmTestList();
  } catch {
    // The next poll tries again; pollStatus shows network problems.
  }
}

async function rescanSensors() {
  try {
    await api("POST", "/api/sensors/scan");
  } catch (error) {
    alertBanner(errorText(error));
  }
  await pollStatus();
  await pollHardware();
}

function alertBanner(text) {
  $("banners").prepend(banner("error", text));
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

// Nothing is polled while the browser tab is hidden, unless hidden() says
// to go on; coming back polls at once.
function every(ms, fn, hidden = () => false) {
  setTimeout(async function tick() {
    if (!document.hidden || hidden()) await fn();
    setTimeout(tick, typeof ms === "function" ? ms() : ms);
  }, typeof ms === "function" ? ms() : ms);
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadI18n();
  applyIcons(document);
  for (const button of document.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }
  $("restart").addEventListener("click", async () => {
    try {
      await api("POST", "/api/restart");
    } catch (error) {
      alertBanner(errorText(error));
    }
    pollStatus();
  });
  $("manual-toggle").addEventListener("click", toggleManual);
  $("smart-refresh").addEventListener("click", refreshSmart);
  $("history-hours").addEventListener("change", loadHistory);
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-form").control_mode.addEventListener("change", renderModeHelp);
  for (const name of ["drive_temp_min", "drive_temp_max", "target_temp", "min_fan_speed_prct"]) {
    $("settings-form")[name].addEventListener("input", renderCurve);
  }
  $("reset-settings").addEventListener("click", resetSettings);
  $("notify-test").addEventListener("click", testNotify);
  $("export-settings").addEventListener("click", () => exportSettings().catch((error) => alertBanner(errorText(error))));
  $("import-settings").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", async () => {
    const file = $("import-file").files[0];
    $("import-file").value = "";
    if (file) await importSettings(file);
  });
  $("pwmtest-start").addEventListener("click", startPwmTest);
  $("pwmtest-cancel").addEventListener("click", cancelPwmTest);
  $("pwmtest-use").addEventListener("click", usePwmResults);
  $("auth-form").addEventListener("submit", saveCredentials);
  $("logout").addEventListener("click", logout);
  for (const button of document.querySelectorAll(".open-wizard")) button.addEventListener("click", openWizard);
  $("log-source").addEventListener("change", renderLogs);
  window.addEventListener("resize", () => {
    if (state.tab === "history") loadHistory();
    if (state.tab === "settings") renderCurve();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollStatus();
      pollHardware();
    }
  });

  try {
    state.labels = await api("GET", "/api/labels");
    await loadSettings();
  } catch {
    // pollStatus shows the problem.
  }
  await Promise.all([pollStatus(), pollHardware(), pollLogs(), loadBays()]);
  let first;
  try {
    first = localStorage.getItem("tab") || "overview";
  } catch {
    first = "overview";
  }
  if (wizardWanted() || first === "setup") openWizard();
  else showTab(document.getElementById(`tab-${first}`) ? first : "overview");

  // In manual control the status keeps coming in a background tab too: the
  // server ends manual control only when the page is really closed.
  every(POLL_STATUS_MS, pollStatus, () => Boolean(state.status && state.status.manual));
  every(() => (state.tab === "fans" || state.tab === "overview" ? POLL_HARDWARE_FAST_MS : POLL_HARDWARE_SLOW_MS), pollHardware);
  every(POLL_LOGS_MS, pollLogs);
  every(POLL_HISTORY_MS, () => state.tab === "history" && loadHistory());
  every(POLL_HISTORY_MS, () => state.tab === "overview" && loadTrend());
  loadTrend();
});
