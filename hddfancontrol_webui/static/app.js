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
};

const POLL_STATUS_MS = 3000;
const POLL_HARDWARE_FAST_MS = 2000;
const POLL_HARDWARE_SLOW_MS = 15000;
const POLL_LOGS_MS = 2000;
const POLL_HISTORY_MS = 60000;
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

// hddfancontrol names drives "sda ST4000VN006": show the label, then the kernel name.
function driveLabelByLogName(name) {
  const kernel = name.split(" ")[0];
  const drive = state.hardware.drives.find((d) => d.name === kernel);
  const label = drive ? driveLabelById(drive.ids[0]) : "";
  return label ? `${label} (${kernel})` : name;
}

// A temperature series of the history: a /dev/disk/by-id path (or, before
// 0.2.0, the "sda MODEL" name of hddfancontrol).
function driveLabelBySeries(name) {
  if (!name.startsWith("/dev/")) return driveLabelByLogName(name);
  const label = driveLabelById(name);
  const drive = state.hardware.drives.find((d) => d.ids.includes(name));
  const now = drive ? `${drive.model} (${drive.name})` : shortPath(name);
  return label ? `${label} (${drive ? drive.name : shortPath(name)})` : now;
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
    box.append(banner(alert.kind === "smart_warning" ? "warning" : "error", alertText(alert), el("div", { class: "banner-actions" }, ackButton(alert.key, alert.acknowledged))));
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
  if (status.fans.safety) box.append(banner("error", t(status.fans.safety_reason === "stale" ? "banner.safety_stale" : "banner.safety")));
}

// cool: below the minimum; mild and warm: the two halves of the range; hot: at the maximum.
function tempLevel(value) {
  const low = state.settings ? state.settings.drive_temp_min : 30;
  const high = state.settings ? state.settings.drive_temp_max : 50;
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
      if (value === null) {
        asleep += 1;
        temps.append(
          el("div", { class: "meter asleep" }, icon("moon"), el("span", { class: "meter-name", text: driveLabelByLogName(name) }), bar(0, "cool"), el("span", { class: "meter-value muted", text: t("state.asleep") })),
        );
        continue;
      }
      values.push(value);
      const level = tempLevel(value);
      temps.append(
        el("div", { class: `meter ${level}` }, icon("drive"), el("span", { class: "meter-name", text: driveLabelByLogName(name) }), bar((value - from) / (to - from), level), el("span", { class: "meter-value", text: fmtTemp(value) })),
      );
    }
  } else {
    $("latest-time").textContent = t("overview.no_temps");
  }
  if (values.length) {
    const hottest = Math.max(...values);
    setStat("stat-hottest", fmtTemp(hottest), tempLevel(hottest));
  } else {
    setStat("stat-hottest", "–");
  }
  const total = values.length + asleep;
  setStat("stat-drives", total ? `${values.length} / ${total}` : "–");
  $("stat-drives").title = asleep ? t("overview.asleep_count", { count: asleep }) : "";

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
  if (status.fans.mode === "auto" && status.fans.auto.speed !== null) {
    control.append(row(t("overview.hottest"), fmtTemp(status.fans.auto.hottest)));
    control.append(row(t("overview.auto_speed"), `${Math.round(status.fans.auto.speed * 100)} %`));
  }
  for (const [name, daemon] of Object.entries(status.daemons)) {
    const text = t(`daemon.${daemon.state}`) + (daemon.restarts ? ` · ${t("overview.restarts", { count: daemon.restarts })}` : "");
    control.append(row(name, text));
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
// Fans: manual control
// ---------------------------------------------------------------------------

function renderFansState() {
  const status = state.status;
  if (!status) return;
  const button = $("manual-toggle");
  button.textContent = status.manual ? t("action.leave_manual") : t("action.enter_manual");
  button.className = status.manual ? "danger" : "";
  button.disabled = status.pwm_test.running || status.read_only;
  $("manual-safety").hidden = !status.fans.safety;
  $("manual-safety").textContent = t(status.fans.safety_reason === "stale" ? "fans.safety_stale" : "fans.safety");
  for (const input of document.querySelectorAll("#fan-list input[type=range]")) {
    input.disabled = !status.manual || status.fans.safety;
  }
}

function sendPwm(path, value) {
  clearTimeout(state.pending.get(path));
  state.pending.set(
    path,
    setTimeout(async () => {
      try {
        await api("PUT", "/api/manual/pwm", { path, value });
      } catch (error) {
        alertBanner(errorText(error));
      }
    }, 120),
  );
}

function renderFans() {
  const list = $("fan-list");
  const manual = state.status ? state.status.manual : false;
  const configured = new Set((state.settings ? state.settings.pwms : []).map((pwm) => pwm.path));
  for (const device of state.hardware.hwmons) {
    if (device.pwms.length === 0) continue;
    let group = list.querySelector(`[data-hwmon="${device.hwmon}"]`);
    if (!group) {
      group = el("div", { class: "fan-group", "data-hwmon": device.hwmon }, el("h3", { text: `${device.name || "?"} (${device.hwmon})` }));
      list.append(group);
    }
    for (const pwm of device.pwms) {
      let line = group.querySelector(`[data-path="${pwm.path}"]`);
      if (!line) {
        const range = el("input", { type: "range", min: 0, max: 255, step: 1, "aria-label": pwm.path });
        range.addEventListener("pointerdown", () => state.dragging.add(pwm.path));
        range.addEventListener("pointerup", () => state.dragging.delete(pwm.path));
        range.addEventListener("input", () => {
          line.querySelector(".pct").textContent = fmtPct(Number(range.value));
          sendPwm(pwm.path, Number(range.value));
        });
        line = el(
          "div",
          { class: "fan-line", "data-path": pwm.path },
          el(
            "div",
            { class: "fan-name" },
            el("strong", { text: shortPath(pwm.path) }),
            el("span", { class: "badge" }),
            labelInput(pwm.path, state.labels[pwm.path]),
            el("small", { class: "muted", text: pwm.path }),
          ),
          range,
          el("span", { class: "pct" }),
          el("span", { class: "rpm" }),
        );
        group.append(line);
      }
      const range = line.querySelector("input[type=range]");
      if (!state.dragging.has(pwm.path) && document.activeElement !== range && pwm.value !== null) {
        range.value = pwm.value;
        line.querySelector(".pct").textContent = fmtPct(pwm.value);
      }
      range.disabled = !manual || (state.status && state.status.fans.safety);
      line.querySelector(".rpm").textContent = fmtRpm(pwm.rpm);
      const badge = line.querySelector(".badge");
      badge.textContent = configured.has(pwm.path) ? t("fans.configured") : "";
      badge.hidden = !configured.has(pwm.path);
    }
  }
  if (!list.children.length) list.replaceChildren(el("p", { class: "muted", text: t("banner.no_pwm") }));
}

async function toggleManual() {
  const enabled = !(state.status && state.status.manual);
  try {
    await api("PUT", "/api/manual", { enabled });
  } catch (error) {
    alertBanner(errorText(error));
  }
  await pollStatus();
  await pollHardware();
}

// ---------------------------------------------------------------------------
// SMART
// ---------------------------------------------------------------------------

function smartCard(device, data) {
  const status = data.status || "error";
  const label = driveLabelById(device);
  const title = el("div", { class: "card-head" }, el("h3", { text: label || shortPath(device) }), el("span", { class: `badge ${status}`, text: t(`smart.status.${status}`) }));
  const card = el("article", { class: "card smart" }, title);
  if (label) card.append(el("p", { class: "muted small", text: shortPath(device) }));
  if (data.model) card.append(el("p", { class: "muted small", text: `${data.model} · ${fmtBytes(data.capacity_bytes)}` }));
  card.append(driveIdentity(device, data));
  const table = el("table", { class: "list" });
  const drive = state.hardware.drives.find((d) => d.ids.includes(device));
  const limit = data.temp_limit ?? null;
  const fields = [
    ["family", data.family],
    ["connection", connectionText(drive, data)],
    ["rpm", data.rpm ? `${data.rpm} RPM` : null],
    ["form_factor", data.form_factor],
    ["temp_limit", limit === null ? null : fmtTemp(limit)],
    ["temp_lifetime_max", data.temp_lifetime_max === undefined || data.temp_lifetime_max === null ? null : fmtTemp(data.temp_lifetime_max)],
    ["serial", data.serial],
    ["firmware", data.firmware],
    ["temperature", data.temperature === undefined || data.temperature === null ? null : fmtTemp(data.temperature)],
    ["power_on_hours", data.power_on_hours === undefined || data.power_on_hours === null ? null : `${data.power_on_hours} h (${Math.round(data.power_on_hours / 24)} d)`],
    ["power_cycles", data.power_cycles],
    ["start_stop_count", data.start_stop_count],
    ["load_cycle_count", data.load_cycle_count],
    ["reallocated_sectors", data.reallocated_sectors],
    ["pending_sectors", data.pending_sectors],
    ["offline_uncorrectable", data.offline_uncorrectable],
    ["reported_uncorrectable", data.reported_uncorrectable],
    ["crc_errors", data.crc_errors],
    ["percentage_used", data.percentage_used === undefined || data.percentage_used === null ? null : `${data.percentage_used} %`],
    ["available_spare", data.available_spare === undefined || data.available_spare === null ? null : `${data.available_spare} %`],
    ["media_errors", data.media_errors],
    ["unsafe_shutdowns", data.unsafe_shutdowns],
  ];
  for (const [key, value] of fields) {
    if (value !== undefined && value !== null && value !== "") table.append(row(t(`smart.${key}`), String(value)));
  }
  card.append(table);
  // The maximum of the settings should stay under what the drive is made for.
  if (limit !== null && state.settings && state.settings.drive_temp_max > limit) {
    card.append(el("p", { class: "warning small", text: t("drive.max_over_limit", { max: fmtTemp(state.settings.drive_temp_max), limit: fmtTemp(limit) }) }));
  }
  for (const warning of data.warnings || []) card.append(el("p", { class: "warning small", text: t(`smart.warn.${warning}`) }));
  for (const message of data.messages || []) card.append(el("p", { class: "error small", text: message }));
  const note = data.asleep ? t("smart.asleep_note") : "";
  card.append(el("p", { class: "muted small", text: `${t("smart.updated", { time: fmtTime(data.time) })} ${note}` }));
  if (data.attributes && data.attributes.length) {
    const attrs = el("table", { class: "list attrs" }, headRow("ID", t("smart.attribute"), t("smart.value"), t("smart.worst"), t("smart.thresh"), t("smart.raw")));
    for (const attr of data.attributes) {
      const line = row(String(attr.id), attr.name, String(attr.value ?? ""), String(attr.worst ?? ""), String(attr.thresh ?? ""), String(attr.raw ?? ""));
      if (attr.failed) line.className = "failed";
      attrs.append(line);
    }
    card.append(el("details", {}, el("summary", { text: t("smart.attributes") }), attrs));
  }
  return card;
}

// Drivers of controllers that are host bus adapters (HBA), not ports of the board.
const HBA_DRIVERS = ["mpt2sas", "mpt3sas", "mpi3mr", "megaraid_sas", "aacraid", "hpsa", "smartpqi", "mvsas", "isci", "pm80xx", "arcmsr"];

// "SATA 6.0 Gb/s · board (ahci)", "SATA via HBA (mpt3sas)", "NVMe 8.0 GT/s PCIe x4".
function connectionText(drive, data) {
  const bus = drive ? drive.bus : "";
  const protocol = (data.protocol || "").toUpperCase();
  let text = { sata: "SATA", sas: protocol === "ATA" ? "SATA" : "SAS", nvme: "NVMe", usb: "USB" }[bus] || (protocol === "ATA" ? "SATA" : protocol);
  if (data.link_now) text += ` ${data.link_now}`;
  if (data.link_max && data.link_now && data.link_max !== data.link_now) text += ` (${t("drive.link_max", { speed: data.link_max })})`;
  if (drive && drive.link) text += ` ${drive.link}`;
  const controller = drive ? drive.controller : "";
  if (controller === "ahci") text += ` · ${t("drive.ctrl_board")}`;
  else if (HBA_DRIVERS.includes(controller)) text += ` · ${t("drive.ctrl_hba", { driver: controller })}`;
  else if (controller && bus !== "nvme") text += ` · ${controller}`;
  return text.trim() || null;
}

// Brand and line, what it is made for, and how it records (SMR is a poor fit for ZFS).
function driveIdentity(device, data) {
  const info = data.catalog || {};
  const box = el("div", { class: "identity" });
  const name = [info.brand, info.line].filter(Boolean).join(" ");
  if (name) box.append(el("span", { class: "badge", text: name }));
  if (info.use) box.append(el("span", { class: "badge", text: t(`drive.use.${info.use}`) }));
  if (info.recording) {
    const badge = el("span", { class: `badge ${info.recording === "SMR" ? "warning" : "ok"}`, text: info.recording });
    if (info.recording === "SMR") badge.title = t("drive.smr_hint");
    box.append(badge);
  }
  return box;
}

function renderSmart(data) {
  const list = $("smart-list");
  const entries = Object.entries(data).sort(([a], [b]) => a.localeCompare(b));
  list.replaceChildren(...entries.map(([device, info]) => smartCard(device, info)));
  if (!entries.length) list.append(el("p", { class: "muted", text: t("smart.none") }));
}

async function loadSmart() {
  try {
    renderSmart(await api("GET", "/api/smart"));
  } catch (error) {
    alertBanner(errorText(error));
  }
}

async function refreshSmart() {
  const button = $("smart-refresh");
  button.disabled = true;
  try {
    renderSmart(await api("POST", "/api/smart"));
  } catch (error) {
    alertBanner(errorText(error));
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// History charts (inline SVG, no library)
// ---------------------------------------------------------------------------

const SVG = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function niceTicks(min, max, count) {
  const span = max - min || 1;
  const raw = span / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * power).find((s) => s >= raw) || raw;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(Number(v.toFixed(6)));
  return ticks;
}

function drawChart(container, data, series, unit, labelOf) {
  container.replaceChildren();
  if (!series.length) {
    container.append(el("p", { class: "muted", text: t("history.empty") }));
    return;
  }
  const width = Math.max(container.clientWidth, 300);
  const height = 240;
  const pad = { left: 52, right: 12, top: 10, bottom: 28 };
  let min = Infinity;
  let max = -Infinity;
  for (const s of series) {
    for (const [, value] of s.points) {
      min = Math.min(min, value);
      max = Math.max(max, value);
    }
  }
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const margin = (max - min) * 0.08;
  min -= margin;
  max += margin;
  const x = (ts) => pad.left + ((ts - data.start) / (data.end - data.start)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + (1 - (value - min) / (max - min)) * (height - pad.top - pad.bottom);

  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });
  for (const tick of niceTicks(min, max, 4)) {
    chart.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: y(tick), y2: y(tick), class: "gridline" }));
    const label = svg("text", { x: pad.left - 6, y: y(tick) + 4, class: "axis", "text-anchor": "end" });
    label.textContent = `${tick}${unit}`;
    chart.append(label);
  }
  const long = data.end - data.start > 2 * 86400;
  for (let i = 0; i <= 4; i += 1) {
    const ts = data.start + ((data.end - data.start) * i) / 4;
    const date = new Date(ts * 1000);
    const label = svg("text", { x: x(ts), y: height - 8, class: "axis", "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" });
    label.textContent = long
      ? date.toLocaleDateString(I18N.lang, { day: "2-digit", month: "2-digit" })
      : date.toLocaleTimeString(I18N.lang, { hour: "2-digit", minute: "2-digit" });
    chart.append(label);
  }
  const legend = el("div", { class: "legend" });
  series.forEach((s, index) => {
    // A class, not a style attribute: the CSP blocks inline styles.
    const color = `c${(index % 8) + 1}`;
    // A gap of more than three buckets (a sleeping drive, a stopped container) breaks the line.
    let path = "";
    let last = null;
    for (const [ts, value] of s.points) {
      const move = last === null || ts - last > 3 * data.bucket;
      path += `${move ? "M" : "L"}${x(ts).toFixed(1)},${y(value).toFixed(1)}`;
      last = ts;
    }
    const line = svg("path", { d: path, class: `series ${color}` });
    const title = svg("title");
    title.textContent = labelOf(s.name);
    line.append(title);
    chart.append(line);
    const lastValue = s.points.length ? s.points[s.points.length - 1][1] : null;
    legend.append(
      el("span", { class: "key" }, el("i", { class: color }), `${labelOf(s.name)}${lastValue === null ? "" : ` · ${lastValue}${unit}`}`),
    );
  });
  container.append(chart, legend);
}

async function loadHistory() {
  const hours = $("history-hours").value;
  try {
    const data = await api("GET", `/api/history?hours=${encodeURIComponent(hours)}`);
    const of = (kind) => data.series.filter((s) => s.kind === kind);
    drawChart($("chart-temp"), data, of("temp"), "°", driveLabelBySeries);
    drawChart($("chart-rpm"), data, of("rpm"), "", pwmLabel);
    drawChart($("chart-pwm"), data, of("pwm"), "%", pwmLabel);
  } catch (error) {
    alertBanner(errorText(error));
  }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function numberInput(name, value, attrs = {}) {
  return el("input", { type: "number", name, value: value ?? "", ...attrs });
}

function renderModeHelp() {
  const mode = $("settings-form").control_mode.value;
  $("mode-help").textContent = t(`settings.mode_help_${mode}`);
  renderCurve();
}

// The fan speed for each drive temperature, as the form now stands, with
// the hottest drive of the last sample as a dot. Curve mode: the minimum
// speed up to the minimum temperature, full speed from the maximum, a line
// in between. Auto mode: the speed is found by feedback, so only the target
// and the maximum (full speed) are drawn.
function renderCurve() {
  const box = $("curve-chart");
  const form = $("settings-form");
  const low = Number(form.drive_temp_min.value);
  const high = Number(form.drive_temp_max.value);
  const target = Number(form.target_temp.value);
  const minSpeed = Math.max(0, Math.min(100, Number(form.min_fan_speed_prct.value)));
  box.replaceChildren();
  if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) return;
  const width = Math.max(box.clientWidth, 300);
  const height = 170;
  const pad = { left: 44, right: 14, top: 10, bottom: 26 };
  const from = Math.floor(low - 10);
  const to = Math.ceil(high + 8);
  const x = (temp) => pad.left + ((temp - from) / (to - from)) * (width - pad.left - pad.right);
  const y = (pct) => pad.top + (1 - pct / 100) * (height - pad.top - pad.bottom);
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height });
  for (const pct of [0, 50, 100]) {
    chart.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: y(pct), y2: y(pct), class: "gridline" }));
    const label = svg("text", { x: pad.left - 6, y: y(pct) + 4, class: "axis", "text-anchor": "end" });
    label.textContent = `${pct}%`;
    chart.append(label);
  }
  for (const temp of niceTicks(from, to, 6)) {
    const label = svg("text", { x: x(temp), y: height - 8, class: "axis", "text-anchor": "middle" });
    label.textContent = `${temp}°`;
    chart.append(label);
  }
  const auto = form.control_mode.value === "auto";
  if (auto) {
    for (const [temp, cls] of [[target, "marker target"], [high, "marker max"]]) {
      if (!Number.isFinite(temp)) continue;
      chart.append(svg("line", { x1: x(temp), x2: x(temp), y1: y(0), y2: y(100), class: cls }));
    }
    chart.append(svg("path", { d: `M${x(high)},${y(100)}H${width - pad.right}`, class: "series c1" }));
  } else {
    chart.append(svg("path", { d: `M${x(from)},${y(minSpeed)}H${x(low)}L${x(high)},${y(100)}H${width - pad.right}`, class: "series c1" }));
  }
  const temps = state.status && state.status.latest ? Object.values(state.status.latest.temps).filter((v) => v !== null) : [];
  // In auto mode the dot sits at the speed the controller chose, when it runs.
  const autoSpeed = state.status && state.status.fans.auto.speed !== null ? state.status.fans.auto.speed * 100 : null;
  if (temps.length && (!auto || autoSpeed !== null)) {
    const hottest = Math.max(...temps);
    const curve = hottest <= low ? minSpeed : hottest >= high ? 100 : minSpeed + ((100 - minSpeed) * (hottest - low)) / (high - low);
    const cx = x(Math.max(from, Math.min(to, hottest)));
    const dot = svg("circle", { cx, cy: y(auto ? autoSpeed : curve), r: 5, class: `now ${tempLevel(hottest)}` });
    const title = svg("title");
    title.textContent = t("settings.curve_now", { temp: fmtTemp(hottest) });
    dot.append(title);
    chart.append(dot);
  }
  box.append(chart);
}

function renderSettings() {
  const settings = state.settings;
  if (!settings) return;
  const form = $("settings-form");
  $("settings-source").textContent = t(`settings.source_${state.settingsSource}`);
  for (const name of ["control_mode", "drive_temp_min", "drive_temp_max", "target_temp", "min_fan_speed_prct", "interval_s", "average", "history_days", "smart_interval_h", "verbosity", "spin_down_time_s", "spin_down_command", "notify_url", "notify_format", "notify_language"]) {
    form[name].value = settings[name];
  }
  form.restore_fan_settings.checked = settings.restore_fan_settings;
  renderModeHelp();

  // Drives: one row per drive found, with the name to use and its own spin down time.
  const drives = $("drive-list");
  drives.replaceChildren(
    headRow(t("settings.drive_controls"), t("settings.drive_monitor"), t("settings.drive"), t("labels.label"), t("settings.drive_name"), t("settings.spin_down_own")),
  );
  const known = new Set();
  for (const drive of state.hardware.drives) {
    drive.ids.forEach((id) => known.add(id));
    const controls = drive.ids.find((id) => settings.drives.includes(id));
    const monitored = drive.ids.find((id) => settings.monitor_drives.includes(id));
    const chosen = controls || monitored;
    const select = el("select", { name: "drive_id" }, ...drive.ids.map((id) => el("option", { value: id, text: shortPath(id), selected: id === chosen })));
    const own = chosen && chosen in settings.spin_down_drives ? settings.spin_down_drives[chosen] : "";
    drives.append(
      el(
        "tr",
        { "data-drive": drive.name },
        el("td", {}, el("input", { type: "checkbox", name: "drive_use", checked: Boolean(controls), "aria-label": `${t("settings.drive_controls")} · ${drive.name}` })),
        el("td", {}, el("input", { type: "checkbox", name: "drive_monitor", checked: Boolean(monitored), "aria-label": `${t("settings.drive_monitor")} · ${drive.name}` })),
        el("td", {}, el("strong", { text: drive.name }), " ", el("span", { class: "muted small", text: `${drive.model} · ${fmtBytes(drive.size_bytes)}${drive.rotational === false ? " · SSD" : ""}` })),
        el("td", {}, labelInput(chosen || drive.ids[0], driveLabelById(drive.ids[0]))),
        el("td", {}, select),
        el("td", {}, numberInput("drive_spin", own, { min: 0, max: 86400, placeholder: settings.spin_down_time_s, "aria-label": t("settings.spin_down_own") })),
      ),
    );
  }
  $("other-drives").value = settings.drives.filter((d) => !known.has(d)).join(" ");

  // Fans: every PWM output found, and the configured ones that are missing.
  const pwms = $("pwm-list");
  pwms.replaceChildren(headRow("", t("fans.output"), t("settings.start"), t("settings.stop"), t("fans.speed")));
  const configured = new Map(settings.pwms.map((pwm) => [pwm.path, pwm]));
  const rows = allPwms().map((pwm) => ({ path: pwm.path, label: pwmLabel(pwm.path), rpm: pwm.rpm }));
  for (const path of configured.keys()) {
    if (!rows.some((r) => r.path === path)) rows.push({ path, label: `${path} (${t("settings.not_found")})`, rpm: null });
  }
  for (const item of rows) {
    const pwm = configured.get(item.path);
    pwms.append(
      el(
        "tr",
        { "data-path": item.path },
        el("td", {}, el("input", { type: "checkbox", name: "pwm_use", checked: Boolean(pwm), "aria-label": item.path })),
        el("td", {}, el("strong", { text: item.label }), el("br"), el("small", { class: "muted", text: item.path })),
        el("td", {}, numberInput("pwm_start", pwm ? pwm.start : "", { min: 0, max: 255, placeholder: "200" })),
        el("td", {}, numberInput("pwm_stop", pwm ? pwm.stop : "", { min: 0, max: 255, placeholder: "75" })),
        el("td", { text: fmtRpm(item.rpm) }),
      ),
    );
  }

  // Extra hwmon temperatures.
  const hwmons = $("hwmon-list");
  hwmons.replaceChildren(headRow("", t("settings.sensor"), t("settings.now"), t("settings.temp_min"), t("settings.temp_max")));
  const chosen = new Map(settings.hwmons.map((h) => [h.path, h]));
  const inputs = state.hardware.hwmons.flatMap((device) => device.temps.map((temp) => ({ ...temp, chip: device.name || device.hwmon })));
  for (const path of chosen.keys()) {
    if (!inputs.some((i) => i.path === path)) inputs.push({ path, label: t("settings.not_found"), chip: "", celsius: null });
  }
  for (const input of inputs) {
    const hwmon = chosen.get(input.path);
    hwmons.append(
      el(
        "tr",
        { "data-path": input.path },
        el("td", {}, el("input", { type: "checkbox", name: "hwmon_use", checked: Boolean(hwmon), "aria-label": input.path })),
        el("td", {}, el("strong", { text: `${input.chip} · ${input.label}` }), el("br"), el("small", { class: "muted", text: input.path })),
        el("td", { text: fmtTemp(input.celsius) }),
        el("td", {}, numberInput("hwmon_min", hwmon ? hwmon.temp_min : "", { step: 0.5, min: 0, max: 120 })),
        el("td", {}, numberInput("hwmon_max", hwmon ? hwmon.temp_max : "", { step: 0.5, min: 0, max: 120 })),
      ),
    );
  }
}

function optionalNumber(value) {
  return value === "" ? null : Number(value);
}

function collectSettings() {
  const form = $("settings-form");
  const doc = {};
  for (const name of ["drive_temp_min", "drive_temp_max", "target_temp", "min_fan_speed_prct", "interval_s", "average", "history_days", "smart_interval_h", "spin_down_time_s"]) {
    doc[name] = form[name].value === "" ? null : Number(form[name].value);
  }
  for (const name of ["control_mode", "verbosity", "spin_down_command", "notify_url", "notify_format", "notify_language"]) doc[name] = form[name].value;
  doc.restore_fan_settings = form.restore_fan_settings.checked;

  doc.drives = [];
  doc.spin_down_drives = {};
  // Monitored drives that are not on the host now stay monitored.
  const found = new Set(state.hardware.drives.flatMap((drive) => drive.ids));
  doc.monitor_drives = (state.settings ? state.settings.monitor_drives : []).filter((id) => !found.has(id));
  for (const line of $("drive-list").querySelectorAll("tr[data-drive]")) {
    const id = line.querySelector("[name=drive_id]").value;
    if (line.querySelector("[name=drive_monitor]").checked) doc.monitor_drives.push(id);
    if (!line.querySelector("[name=drive_use]").checked) continue;
    doc.drives.push(id);
    const own = line.querySelector("[name=drive_spin]").value;
    if (own !== "") doc.spin_down_drives[id] = Number(own);
  }
  for (const other of $("other-drives").value.split(/\s+/).filter(Boolean)) {
    doc.drives.push(other);
    if (state.settings && other in state.settings.spin_down_drives) doc.spin_down_drives[other] = state.settings.spin_down_drives[other];
  }

  doc.pwms = [];
  for (const line of $("pwm-list").querySelectorAll("tr[data-path]")) {
    if (!line.querySelector("[name=pwm_use]").checked) continue;
    doc.pwms.push({
      path: line.dataset.path,
      start: optionalNumber(line.querySelector("[name=pwm_start]").value),
      stop: optionalNumber(line.querySelector("[name=pwm_stop]").value),
    });
  }

  doc.hwmons = [];
  for (const line of $("hwmon-list").querySelectorAll("tr[data-path]")) {
    if (!line.querySelector("[name=hwmon_use]").checked) continue;
    const hwmon = { path: line.dataset.path };
    const low = optionalNumber(line.querySelector("[name=hwmon_min]").value);
    const high = optionalNumber(line.querySelector("[name=hwmon_max]").value);
    if (low !== null) hwmon.temp_min = low;
    if (high !== null) hwmon.temp_max = high;
    doc.hwmons.push(hwmon);
  }
  return doc;
}

function showProblems(problems) {
  for (const node of document.querySelectorAll("[data-error]")) node.textContent = "";
  for (const problem of problems) {
    const exact = document.querySelector(`[data-error="${CSS.escape(problem.field)}"]`);
    const target = exact || document.querySelector(`[data-error="${CSS.escape(problem.field.split(".")[0])}"]`);
    const text = t(`error.${problem.code}`, problem.params || {});
    if (target) target.textContent = target.textContent ? `${target.textContent} ${text}` : `${problem.field}: ${text}`;
  }
}

async function loadSettings() {
  const data = await api("GET", "/api/settings");
  state.settings = data.settings;
  state.settingsSource = data.source;
}

async function saveSettings(event) {
  event.preventDefault();
  const result = $("save-result");
  result.textContent = "";
  showProblems([]);
  try {
    const data = await api("PUT", "/api/settings", collectSettings());
    state.settings = data.settings;
    state.settingsSource = data.source;
    renderSettings();
    result.textContent = t("settings.saved");
    result.className = "small ok";
  } catch (error) {
    if (error.data && error.data.problems) showProblems(error.data.problems);
    result.textContent = errorText(error);
    result.className = "small error";
  }
  pollStatus();
}

// Deleting the saved settings cannot be undone: the first click only arms the
// button for a few seconds, the second one deletes.
let resetArmed = null;

function disarmReset() {
  clearTimeout(resetArmed);
  resetArmed = null;
  const button = $("reset-settings");
  button.textContent = t("action.reset");
  button.className = "secondary";
}

async function resetSettings() {
  const button = $("reset-settings");
  if (resetArmed === null) {
    button.textContent = t("action.reset_confirm");
    button.className = "danger";
    resetArmed = setTimeout(disarmReset, 5000);
    return;
  }
  disarmReset();
  const result = $("save-result");
  try {
    const data = await api("DELETE", "/api/settings");
    state.settings = data.settings;
    state.settingsSource = data.source;
    renderSettings();
    result.textContent = t("settings.reset_done");
    result.className = "small ok";
  } catch (error) {
    result.textContent = errorText(error);
    result.className = "small error";
  }
}

async function testNotify() {
  const result = $("notify-result");
  const button = $("notify-test");
  button.disabled = true;
  result.textContent = "";
  try {
    await api("POST", "/api/notify/test");
    result.textContent = t("settings.notify_sent");
    result.className = "small ok";
  } catch (error) {
    result.textContent = errorText(error);
    result.className = "small error";
  } finally {
    button.disabled = false;
  }
}

// A backup is one JSON file with the saved settings and the names.
const BACKUP_KIND = "hddfancontrol-webui-backup";

async function exportSettings() {
  const data = await api("GET", "/api/settings");
  const labels = await api("GET", "/api/labels");
  const doc = { kind: BACKUP_KIND, version: 1, exported: new Date().toISOString(), settings: data.settings, labels };
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
  const link = el("a", { href: URL.createObjectURL(blob), download: `hddfancontrol-settings-${new Date().toISOString().slice(0, 10)}.json` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

async function importSettings(file) {
  const result = $("backup-result");
  result.className = "small error";
  let doc;
  try {
    doc = JSON.parse(await file.text());
  } catch {
    result.textContent = t("error.backup_invalid");
    return;
  }
  if (!doc || doc.kind !== BACKUP_KIND || typeof doc.settings !== "object") {
    result.textContent = t("error.backup_invalid");
    return;
  }
  try {
    // The server checks the settings as if they were typed in the form.
    const data = await api("PUT", "/api/settings", doc.settings);
    state.settings = data.settings;
    state.settingsSource = data.source;
    for (const [key, label] of Object.entries(doc.labels || {})) {
      if (typeof label === "string") state.labels = await api("PUT", "/api/labels", { key, label });
    }
  } catch (error) {
    if (error.data && error.data.problems) showProblems(error.data.problems);
    result.textContent = errorText(error);
    return;
  }
  renderSettings();
  result.textContent = t("settings.imported");
  result.className = "small ok";
  pollStatus();
}

// ---------------------------------------------------------------------------
// PWM test
// ---------------------------------------------------------------------------

function renderPwmTestList() {
  const list = $("pwmtest-list");
  const checked = new Set([...list.querySelectorAll("input:checked")].map((i) => i.value));
  list.replaceChildren(
    ...allPwms().map((pwm) =>
      el("label", { class: "check" }, el("input", { type: "checkbox", value: pwm.path, checked: checked.has(pwm.path) }), `${pwmLabel(pwm.path)} — ${fmtRpm(pwm.rpm)}`),
    ),
  );
}

function renderPwmTest() {
  const test = state.status.pwm_test;
  $("pwmtest-start").disabled = test.running || state.status.read_only;
  $("pwmtest-cancel").disabled = !test.running;
  const table = $("pwmtest-results");
  table.replaceChildren(headRow(t("fans.output"), t("pwmtest.status"), t("settings.start"), t("settings.stop")));
  for (const path of test.paths) {
    const result = test.results[path] || { status: "pending" };
    const status = t(`pwmtest.${result.status}`) + (result.error ? ` (${result.error})` : "") + (test.current === path ? " …" : "");
    table.append(row(pwmLabel(path), status, String(result.start ?? "–"), String(result.stop ?? "–")));
  }
  $("pwmtest-use").disabled = !Object.values(test.results).some((r) => r.status === "done");
}

async function startPwmTest() {
  const paths = [...$("pwmtest-list").querySelectorAll("input:checked")].map((i) => i.value);
  if (!paths.length) return;
  try {
    await api("POST", "/api/pwm-test", { paths });
  } catch (error) {
    alertBanner(errorText(error));
  }
  pollStatus();
}

async function cancelPwmTest() {
  try {
    await api("DELETE", "/api/pwm-test");
  } catch (error) {
    alertBanner(errorText(error));
  }
  pollStatus();
}

function usePwmResults() {
  showTab("settings");
  const results = state.status.pwm_test.results;
  for (const [path, result] of Object.entries(results)) {
    if (result.status !== "done") continue;
    const line = $("pwm-list").querySelector(`tr[data-path="${CSS.escape(path)}"]`);
    if (!line) continue;
    line.querySelector("[name=pwm_use]").checked = true;
    line.querySelector("[name=pwm_start]").value = result.start;
    line.querySelector("[name=pwm_stop]").value = result.stop;
  }
  const result = $("save-result");
  result.textContent = t("pwmtest.review");
  result.className = "small";
}

// ---------------------------------------------------------------------------
// Security
// ---------------------------------------------------------------------------

async function loadAuth() {
  try {
    const auth = await api("GET", "/api/auth");
    const form = $("auth-form");
    $("security-state").textContent = t(`security.state_${auth.source}`, { username: auth.username });
    form.hidden = auth.source === "environment";
    $("current-field").hidden = !auth.enabled;
    $("auth-remove").hidden = !auth.enabled;
    form.username.value = auth.username || "admin";
  } catch (error) {
    alertBanner(errorText(error));
  }
}

async function saveCredentials(event) {
  event.preventDefault();
  const form = $("auth-form");
  const result = $("auth-result");
  result.className = "small error";
  if (form.password.value !== form.confirm.value) {
    result.textContent = t("error.password_mismatch");
    return;
  }
  try {
    await api("PUT", "/api/auth", { username: form.username.value, password: form.password.value, current: form.current.value });
    form.reset();
    result.textContent = t("security.saved");
    result.className = "small ok";
  } catch (error) {
    result.textContent = errorText(error);
  }
  loadAuth();
  pollStatus();
}

async function removeCredentials() {
  const form = $("auth-form");
  const result = $("auth-result");
  try {
    await api("DELETE", "/api/auth", { current: form.current.value });
    form.reset();
    result.textContent = t("security.removed");
    result.className = "small ok";
  } catch (error) {
    result.textContent = errorText(error);
    result.className = "small error";
  }
  loadAuth();
  pollStatus();
}

async function logout() {
  try {
    await api("POST", "/api/logout");
  } finally {
    location.href = "/login";
  }
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

function formatLog(line) {
  const time = new Date(line.time).toLocaleTimeString(I18N.lang);
  return `${time} [${line.source}] ${line.text}\n`;
}

function renderLogs() {
  const source = $("log-source").value;
  const lines = state.logLines.filter((line) => !source || line.source === source);
  $("log").textContent = lines.map(formatLog).join("");
  $("log").scrollTop = $("log").scrollHeight;
}

async function pollLogs() {
  try {
    const data = await api("GET", `/api/logs?after=${state.logSeq}`);
    if (!data.lines.length) return;
    state.logSeq = data.last;
    state.logLines.push(...data.lines);
    const trimmed = state.logLines.length > LOG_MAX_LINES;
    if (trimmed) state.logLines.splice(0, state.logLines.length - LOG_MAX_LINES);
    if (state.tab !== "logs") return;
    // Appending only would let the page keep every line ever shown.
    if (trimmed) {
      renderLogs();
      return;
    }
    const log = $("log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const source = $("log-source").value;
    log.append(data.lines.filter((line) => !source || line.source === source).map(formatLog).join(""));
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch {
    // The next poll tries again.
  }
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

// Nothing is polled while the browser tab is hidden; coming back polls at once.
function every(ms, fn) {
  setTimeout(async function tick() {
    if (!document.hidden) await fn();
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
  $("auth-remove").addEventListener("click", removeCredentials);
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
  await Promise.all([pollStatus(), pollHardware(), pollLogs()]);
  let first;
  try {
    first = localStorage.getItem("tab") || "overview";
  } catch {
    first = "overview";
  }
  if (wizardWanted() || first === "setup") openWizard();
  else showTab(document.getElementById(`tab-${first}`) ? first : "overview");

  every(POLL_STATUS_MS, pollStatus);
  every(() => (state.tab === "fans" || state.tab === "overview" ? POLL_HARDWARE_FAST_MS : POLL_HARDWARE_SLOW_MS), pollHardware);
  every(POLL_LOGS_MS, pollLogs);
  every(POLL_HISTORY_MS, () => state.tab === "history" && loadHistory());
});
