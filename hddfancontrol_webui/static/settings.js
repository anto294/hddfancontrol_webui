"use strict";

// The Settings tab: the form, the curve chart, reset, notifications and backup.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

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
  for (const name of ["control_mode", "drive_temp_min", "drive_temp_max", "target_temp", "min_fan_speed_prct", "interval_s", "average", "history_days", "verbosity", "spin_down_time_s", "spin_down_command", "notify_url", "notify_format", "notify_language", "notify_repeat_h", ...CRON_FIELDS]) {
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
  pwms.replaceChildren(headRow(t("settings.use"), t("fans.output"), t("settings.start"), t("settings.stop"), t("fans.speed")));
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

  for (const id of ["drive-list", "pwm-list"]) labelCells($(id));
  renderGroups(settings);
  renderLimits(settings);
  // The hints of the limits need the SMART data, read once.
  if (!state.smartData) {
    api("GET", "/api/smart")
      .then((data) => {
        state.smartData = data;
        renderLimits();
      })
      .catch(() => {});
  }

  // Extra hwmon temperatures.
  const hwmons = $("hwmon-list");
  hwmons.replaceChildren(headRow(t("settings.use"), t("settings.sensor"), t("settings.now"), t("settings.temp_min"), t("settings.temp_max")));
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
  labelCells($("hwmon-list"));
  $("hwmon-chosen").textContent = chosen.size ? t("settings.hwmons_chosen", { count: chosen.size }) : "";
  const own = Object.keys(settings.drive_limits || {}).length;
  $("limit-chosen").textContent = own ? t("settings.limits_chosen", { count: own }) : "";
  $("advanced-summary").textContent = t("settings.advanced_summary", { interval: settings.interval_s, days: settings.history_days });
  renderCron();
}

// The drives ticked in the drive table now, as {kernel, id, name}.
function chosenDrives() {
  const chosen = [];
  for (const line of $("drive-list").querySelectorAll("tr[data-drive]")) {
    if (!line.querySelector("[name=drive_use]").checked) continue;
    const drive = state.hardware.drives.find((d) => d.name === line.dataset.drive);
    const id = line.querySelector("[name=drive_id]").value;
    chosen.push({ kernel: line.dataset.drive, id, name: driveLabelById(id) || shortModel(drive ? drive.model : "") || line.dataset.drive });
  }
  return chosen;
}

// The kernel name of a drive of the settings (any of its stable names).
function kernelOf(id) {
  const drive = state.hardware.drives.find((d) => d.ids.includes(id) || `/dev/${d.name}` === id);
  return drive ? drive.name : null;
}

// Fan groups: for each fan ticked, the drives it follows (none ticked = every drive).
// The ticks typed so far survive a rebuild (a drive or fan ticked in the tables above).
function renderGroups(settings) {
  const box = $("group-list");
  const typed = new Map([...box.querySelectorAll("[data-fan]")].map((group) => [group.dataset.fan, new Set([...group.querySelectorAll("input:checked")].map((i) => i.value))]));
  const fresh = settings !== undefined;
  box.replaceChildren();
  const drives = chosenDrives();
  const fans = [...$("pwm-list").querySelectorAll("tr[data-path]")].filter((line) => line.querySelector("[name=pwm_use]").checked).map((line) => line.dataset.path);
  if (!fans.length || drives.length < 2) {
    box.append(el("p", { class: "muted small", text: t("settings.groups_none") }));
    return;
  }
  for (const path of fans) {
    const saved = (state.settings ? state.settings.pwms : []).find((pwm) => pwm.path === path);
    const ticked = !fresh && typed.has(path) ? typed.get(path) : new Set((saved ? saved.drives : []).map(kernelOf).filter(Boolean));
    const group = el("fieldset", { class: "group", "data-fan": path }, el("legend", { text: pwmLabel(path) }));
    for (const drive of drives) {
      group.append(el("label", { class: "chip" }, el("input", { type: "checkbox", value: drive.kernel, checked: ticked.has(drive.kernel) }), ` ${drive.name} `, el("small", { class: "muted", text: drive.kernel })));
    }
    group.append(el("small", { class: "muted group-note", text: t("settings.groups_all") }));
    box.append(group);
  }
}

// Limits per drive: own minimum, target and maximum (empty = the common one),
// with the highest temperature the drive itself recommends as a hint.
function renderLimits(settings) {
  const table = $("limit-list");
  const typed = new Map([...table.querySelectorAll("tr[data-drive]")].map((line) => [line.dataset.drive, [...line.querySelectorAll("input")].map((i) => i.value)]));
  const fresh = settings !== undefined;
  const form = $("settings-form");
  table.replaceChildren(headRow(t("settings.drive"), t("settings.limit_min"), t("settings.limit_target"), t("settings.limit_max"), t("settings.limit_drive")));
  for (const drive of chosenDrives()) {
    const hardware = state.hardware.drives.find((d) => d.name === drive.kernel);
    const ids = hardware ? hardware.ids : [drive.id];
    const ownKey = state.settings ? Object.keys(state.settings.drive_limits || {}).find((id) => ids.includes(id)) : undefined;
    const own = ownKey ? state.settings.drive_limits[ownKey] : {};
    const values = !fresh && typed.has(drive.kernel) ? typed.get(drive.kernel) : ["min", "target", "max"].map((k) => own[k] ?? "");
    const smart = state.smartData ? Object.entries(state.smartData).find(([id]) => ids.includes(id)) : undefined;
    const recommended = smart ? smart[1].temp_limit ?? null : null;
    const max = numberInput("limit_max", values[2], { step: 0.5, min: 0, max: 120, placeholder: form.drive_temp_max.value, "aria-label": `${t("settings.limit_max")} · ${drive.name}` });
    const hint =
      recommended === null
        ? el("span", { class: "muted", text: "–" })
        : el("button", { type: "button", class: "link", text: t("settings.limit_use", { temp: fmtTemp(recommended) }), title: t("settings.limit_use_help"), onclick: () => (max.value = recommended) });
    table.append(
      el(
        "tr",
        { "data-drive": drive.kernel, "data-id": drive.id },
        el("td", {}, el("strong", { text: drive.name }), " ", el("small", { class: "muted", text: drive.kernel })),
        el("td", {}, numberInput("limit_min", values[0], { step: 0.5, min: 0, max: 120, placeholder: form.drive_temp_min.value, "aria-label": `${t("settings.limit_min")} · ${drive.name}` })),
        el("td", {}, numberInput("limit_target", values[1], { step: 0.5, min: 0, max: 120, placeholder: form.target_temp.value, "aria-label": `${t("settings.limit_target")} · ${drive.name}` })),
        el("td", {}, max),
        el("td", {}, hint),
      ),
    );
  }
  if (!table.querySelector("tr[data-drive]")) table.replaceChildren(el("tr", {}, el("td", { class: "muted small", text: t("settings.limits_none") })));
  labelCells(table);
}

// Each cell of a form table gets the title of its column (data-label): on a
// phone the rows become stacked blocks, and CSS shows the title over each field.
function labelCells(table) {
  const titles = [...table.querySelectorAll("tr:first-child th")].map((th) => th.textContent);
  for (const line of table.querySelectorAll("tr")) {
    [...line.children].forEach((cell, index) => {
      if (cell.tagName === "TD") cell.dataset.label = titles[index] || "";
      // The cell that names the row takes the whole width.
      if (cell.querySelector("strong")) cell.classList.add("row-name");
    });
  }
}

function optionalNumber(value) {
  return value === "" ? null : Number(value);
}

function collectSettings() {
  const form = $("settings-form");
  const doc = {};
  for (const name of ["drive_temp_min", "drive_temp_max", "target_temp", "min_fan_speed_prct", "interval_s", "average", "history_days", "spin_down_time_s", "notify_repeat_h"]) {
    doc[name] = form[name].value === "" ? null : Number(form[name].value);
  }
  for (const name of ["control_mode", "verbosity", "spin_down_command", "notify_url", "notify_format", "notify_language"]) doc[name] = form[name].value;
  for (const name of CRON_FIELDS) doc[name] = form[name].value.trim();
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
    // The drives this fan follows, by the stable name chosen above; all ticked is the same as none.
    const group = $("group-list").querySelector(`[data-fan="${CSS.escape(line.dataset.path)}"]`);
    const ticked = group ? [...group.querySelectorAll("input:checked")].map((i) => i.value) : [];
    const drives = chosenDrives();
    const follows = ticked.length === drives.length ? [] : drives.filter((d) => ticked.includes(d.kernel)).map((d) => d.id);
    doc.pwms.push({
      path: line.dataset.path,
      start: optionalNumber(line.querySelector("[name=pwm_start]").value),
      stop: optionalNumber(line.querySelector("[name=pwm_stop]").value),
      drives: follows,
    });
  }

  doc.drive_limits = {};
  for (const line of $("limit-list").querySelectorAll("tr[data-drive]")) {
    const own = {};
    for (const key of ["min", "target", "max"]) {
      const value = line.querySelector(`[name=limit_${key}]`).value;
      if (value !== "") own[key] = Number(value);
    }
    if (Object.keys(own).length) doc.drive_limits[line.dataset.id] = own;
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
  openFoldsWithErrors();
}

// A problem in a folded section opens it, so the field at fault is in sight.
function openFoldsWithErrors() {
  for (const fold of document.querySelectorAll("#settings-form details.card-fold")) {
    if ([...fold.querySelectorAll("[data-error]")].some((node) => node.textContent)) fold.open = true;
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

// --- schedules: cron expressions -----------------------------------------

const CRON_FIELDS = ["smart_schedule", "selftest_short", "selftest_long", "notify_summary", "notify_quiet"];
// Ready-made expressions offered by each field; any other can be typed.
const CRON_PRESETS = {
  smart_schedule: [["0 */6 * * *", "cron.every_six_hours"], ["0 * * * *", "cron.every_hour"], ["0 3 * * *", "cron.every_night"]],
  selftest_short: [["0 3 * * 0", "cron.every_sunday"], ["0 3 * * *", "cron.every_night"]],
  selftest_long: [["0 3 1 * *", "cron.every_month"], ["0 3 1,15 * *", "cron.twice_a_month"]],
  notify_summary: [["0 9 * * *", "cron.every_morning"], ["0 9 * * 1", "cron.every_monday"]],
  notify_quiet: [["* 22-23,0-6 * * *", "cron.nights"], ["* 22-23,0-8 * * 0,6", "cron.weekend_nights"]],
};
const CRON_DELAY_MS = 300;
const cronTimers = {};

// The server sends its own wall clock ("2026-11-22T03:00"): shown as is, whatever the zone of the browser.
function cronWall(text, timeOnly = false) {
  const date = new Date(text);
  const time = { hour: "2-digit", minute: "2-digit" };
  return timeOnly ? date.toLocaleTimeString(I18N.lang, time) : date.toLocaleString(I18N.lang, { weekday: "short", day: "numeric", month: "short", ...time });
}

async function previewCron(name) {
  const input = $("settings-form")[name];
  const out = document.querySelector(`[data-cron-preview="${name}"]`);
  const error = document.querySelector(`[data-error="${name}"]`);
  const expr = input.value.trim();
  error.textContent = "";
  if (!expr) {
    out.textContent = t("cron.never");
    return;
  }
  const stretches = name === "notify_quiet";
  try {
    const data = await api("GET", `/api/cron?expr=${encodeURIComponent(expr)}${stretches ? "&windows=1" : ""}`);
    if (input.value.trim() !== expr) return;
    const items = stretches ? data.windows.map(([start, end]) => `${cronWall(start)} – ${cronWall(end, start.slice(0, 10) === end.slice(0, 10))}`) : data.runs.map((run) => cronWall(run));
    out.textContent = items.length ? t("cron.next", { times: items.join(" · "), zone: data.zone }) : t("cron.not_soon");
  } catch (err) {
    if (input.value.trim() !== expr) return;
    out.textContent = "";
    const problem = err.data && err.data.problems && err.data.problems[0];
    error.textContent = problem ? t(`error.${problem.code}`, problem.params || {}) : errorText(err);
  }
}

function renderCron() {
  for (const name of CRON_FIELDS) {
    $(`cron-${name}`).replaceChildren(...CRON_PRESETS[name].map(([value, key]) => el("option", { value, label: t(key) })));
    previewCron(name);
  }
}

// The groups and the limits follow the drives and fans ticked above.
document.addEventListener("DOMContentLoaded", () => {
  // The browser cannot show a field it refuses (a number out of range) in a folded section.
  $("settings-form").addEventListener(
    "invalid",
    (event) => {
      const fold = event.target.closest("details");
      if (fold) fold.open = true;
    },
    true,
  );
  for (const name of CRON_FIELDS) {
    $("settings-form")[name].addEventListener("input", () => {
      clearTimeout(cronTimers[name]);
      cronTimers[name] = setTimeout(() => previewCron(name), CRON_DELAY_MS);
    });
  }
  for (const id of ["drive-list", "pwm-list"]) {
    $(id).addEventListener("change", () => {
      renderGroups();
      renderLimits();
    });
  }
});
