"use strict";

// The SMART tab: one card per monitored drive, with what it is and how it is connected.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

// ---------------------------------------------------------------------------
// SMART
// ---------------------------------------------------------------------------

// Counters that should stay at 0: shown up front when they are not.
const SMART_ERRORS = ["reallocated_sectors", "pending_sectors", "offline_uncorrectable", "reported_uncorrectable", "crc_errors", "media_errors"];
// Cards with problems come first.
const SMART_ORDER = { failing: 0, warning: 1, error: 2, standby: 3, ok: 4 };

const present = (value) => value !== undefined && value !== null && value !== "";

// "Seagate Exos X16 · ZL2L1F91": the name given to the drive, or its family
// (or model) and serial number, instead of the long /dev/disk/by-id name.
function smartTitle(device, data) {
  const label = driveLabelById(device);
  if (label) return label;
  const what = data.family || shortModel(data.model) || shortPath(device);
  return data.serial ? `${what} · ${data.serial}` : what;
}

function smartCard(device, data) {
  const status = data.status || "error";
  const drive = state.hardware.drives.find((d) => d.ids.includes(device));
  const card = el(
    "article",
    { class: `card smart smart-${status}` },
    el("div", { class: "card-head" }, el("h3", { text: smartTitle(device, data), title: device }), el("span", { class: `badge ${status}`, text: t(`smart.status.${status}`) })),
  );
  const sub = [shortModel(data.model), data.capacity_bytes ? fmtBytes(data.capacity_bytes) : "", drive ? drive.name : ""].filter(Boolean).join(" · ");
  if (sub) card.append(el("p", { class: "muted small", text: sub }));
  card.append(driveIdentity(device, data));

  // What matters every day, up front; the rest under Details.
  const limit = data.temp_limit ?? null;
  const facts = el("div", { class: "facts" });
  const fact = (label, value, level = "") => facts.append(el("div", { class: `fact ${level}`.trim() }, el("span", { class: "muted small", text: label }), el("strong", { text: value })));
  if (present(data.temperature)) fact(t("smart.temperature"), fmtTemp(data.temperature), tempLevel(data.temperature, (state.hardware.drives.find((dr) => dr.ids.includes(device)) || {}).name));
  if (present(data.temp_lifetime_max)) fact(t("smart.temp_lifetime_max_short"), fmtTemp(data.temp_lifetime_max), limit !== null && data.temp_lifetime_max > limit ? "warm" : "");
  if (limit !== null) fact(t("smart.temp_limit_short"), fmtTemp(limit));
  if (present(data.power_on_hours)) fact(t("smart.power_on_hours"), t("smart.years", { years: (data.power_on_hours / 8766).toFixed(1) }));
  if (present(data.percentage_used)) fact(t("smart.percentage_used"), `${data.percentage_used} %`, data.percentage_used >= 90 ? "hot" : "");
  for (const key of SMART_ERRORS) {
    if (data[key]) fact(t(`smart.${key}`), String(data[key]), "hot");
  }
  const test = data.selftest;
  if (test) {
    const result = test.running ? t("smart.selftest_running") : test.passed === true ? t("smart.selftest_ok") : test.passed === false ? t("smart.selftest_failed") : test.result;
    fact(t("smart.selftest"), result, test.passed === false ? "hot" : "");
  }
  if (facts.children.length) card.append(facts);

  // The maximum of the settings should stay under what the drive is made for.
  if (limit !== null && state.settings && state.settings.drive_temp_max > limit) {
    card.append(el("p", { class: "warning small", text: t("drive.max_over_limit", { max: fmtTemp(state.settings.drive_temp_max), limit: fmtTemp(limit) }) }));
  }
  for (const warning of data.warnings || []) card.append(el("p", { class: "warning small", text: t(`smart.warn.${warning}`) }));
  if (data.failed_past && data.failed_past.length) card.append(el("p", { class: "muted small", text: t("smart.failed_past", { attributes: data.failed_past.join(", ") }) }));
  for (const message of data.messages || []) card.append(el("p", { class: "error small", text: message }));

  const table = el("table", { class: "list" });
  const fields = [
    ["family", data.family],
    ["connection", connectionText(drive, data)],
    ["rpm", data.rpm ? `${data.rpm} RPM` : null],
    ["form_factor", data.form_factor],
    ["temp_limit", limit === null ? null : fmtTemp(limit)],
    ["temp_lifetime_max", present(data.temp_lifetime_max) ? fmtTemp(data.temp_lifetime_max) : null],
    ["serial", data.serial],
    ["firmware", data.firmware],
    ["power_on_hours", present(data.power_on_hours) ? `${data.power_on_hours} h (${Math.round(data.power_on_hours / 24)} d)` : null],
    ["power_cycles", data.power_cycles],
    ["start_stop_count", data.start_stop_count],
    ["load_cycle_count", data.load_cycle_count],
    ...SMART_ERRORS.map((key) => [key, data[key]]),
    ["available_spare", present(data.available_spare) ? `${data.available_spare} %` : null],
    ["selftest_last", test ? [test.type, test.result, present(test.hours) ? `${test.hours} h` : ""].filter(Boolean).join(" · ") : null],
    ["unsafe_shutdowns", data.unsafe_shutdowns],
  ];
  for (const [key, value] of fields) {
    if (present(value)) table.append(row(t(`smart.${key}`), String(value)));
  }
  const details = el("details", {}, el("summary", { text: t("smart.details") }), table, el("p", { class: "muted small", text: device }));
  if (data.attributes && data.attributes.length) {
    const attrs = el("table", { class: "list attrs" }, headRow("ID", t("smart.attribute"), t("smart.value"), t("smart.worst"), t("smart.thresh"), t("smart.raw")));
    for (const attr of data.attributes) {
      const line = row(String(attr.id), attr.name, String(attr.value ?? ""), String(attr.worst ?? ""), String(attr.thresh ?? ""), String(attr.raw ?? ""));
      // Data saved before failed_now existed: any when_failed counts.
      const now = attr.failed_now ?? Boolean(attr.failed);
      if (attr.failed) line.className = now ? "failed" : "failed-past";
      attrs.append(line);
    }
    details.append(el("h4", { text: t("smart.attributes") }), attrs);
  }
  card.append(details);
  const note = data.asleep ? t("smart.asleep_note") : "";
  card.append(el("p", { class: "muted small", text: `${t("smart.updated", { time: fmtTime(data.time) })} ${note}` }));
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
  state.smartData = data;
  const list = $("smart-list");
  const rank = ([device, info]) => [SMART_ORDER[info.status] ?? 2, smartTitle(device, info)];
  const entries = Object.entries(data).sort((a, b) => {
    const [ra, na] = rank(a);
    const [rb, nb] = rank(b);
    return ra - rb || na.localeCompare(nb);
  });
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
// Bay map: where each drive sits in the case
// ---------------------------------------------------------------------------

// The drive of the host behind a stable name, and its temperature entry.
function bayDrive(id) {
  const drive = state.hardware.drives.find((d) => d.ids.includes(id) || `/dev/${d.name}` === id);
  const temps = state.status && state.status.latest ? state.status.latest.temps : {};
  const entry = drive ? Object.entries(temps).find(([name]) => name.split(" ")[0] === drive.name) : undefined;
  return { drive, logName: entry ? entry[0] : drive ? drive.name : "", temp: entry ? entry[1] : undefined };
}

// The bays as a grid; each holds the name and the temperature of its drive.
function renderBays() {
  const bays = state.bays;
  const card = $("bay-card");
  const shown = bays && bays.rows * bays.cols > 0 && bays.slots.some(Boolean);
  card.hidden = !shown;
  if (!shown) return;
  const box = $("bay-map");
  box.className = `bays cols-${bays.cols}`;
  box.replaceChildren(
    ...bays.slots.map((id, index) => {
      if (!id) return el("div", { class: "bay empty" }, el("span", { class: "muted small", text: t("bays.empty", { number: index + 1 }) }));
      const { drive, logName, temp } = bayDrive(id);
      const name = drive ? driveTitle(logName).name : shortPath(id);
      const smart = state.smartData && state.smartData[id] ? state.smartData[id].status : "";
      const level = temp === undefined ? "gone" : temp === null ? "asleep" : tempLevel(temp, drive.name);
      const value = temp === undefined ? t("bays.missing") : temp === null ? t("state.asleep") : fmtTemp(temp);
      return el(
        "div",
        { class: `bay ${level}${smart === "failing" || smart === "warning" ? ` smart-${smart}` : ""}`, title: id },
        el("span", { class: "bay-number muted", text: String(index + 1) }),
        el("strong", { class: "bay-name", text: name }),
        el("small", { class: "muted", text: drive ? drive.name : "" }),
        el("span", { class: "bay-temp", text: value }),
      );
    }),
  );
}

// The editor: rows and columns, and a list of drives per bay.
function renderBayEditor() {
  const bays = state.bays || { rows: 0, cols: 0, slots: [] };
  const rows = Math.max(0, Math.min(12, Number($("bay-rows").value) || 0));
  const cols = Math.max(0, Math.min(12, Number($("bay-cols").value) || 0));
  const kept = [...$("bay-editor").querySelectorAll("select")].map((select) => select.value);
  const before = kept.length ? kept : bays.slots;
  const box = $("bay-editor");
  box.className = `bays cols-${cols}`;
  const choices = state.hardware.drives.map((drive) => {
    // The stable name the settings use for the drive, else its first one.
    const chosen = state.settings ? drive.ids.find((id) => state.settings.drives.includes(id) || state.settings.monitor_drives.includes(id)) : undefined;
    return { id: chosen || drive.ids[0], ids: drive.ids, text: `${driveLabelById(drive.ids[0]) || shortModel(drive.model)} · ${drive.name}` };
  });
  const slots = [];
  for (let index = 0; index < rows * cols; index += 1) {
    const current = before[index] || "";
    const select = el("select", { "aria-label": t("bays.empty", { number: index + 1 }) }, el("option", { value: "", text: "—" }));
    for (const choice of choices) select.append(el("option", { value: choice.id, text: choice.text, selected: choice.ids.includes(current) }));
    slots.push(el("div", { class: "bay" }, el("span", { class: "bay-number muted", text: String(index + 1) }), select));
  }
  box.replaceChildren(...slots);
}

async function saveBays() {
  const rows = Number($("bay-rows").value) || 0;
  const cols = Number($("bay-cols").value) || 0;
  const slots = [...$("bay-editor").querySelectorAll("select")].map((select) => select.value);
  try {
    state.bays = await api("PUT", "/api/bays", { rows, cols, slots });
    $("bay-result").textContent = t("bays.saved");
    renderBays();
  } catch (error) {
    $("bay-result").textContent = errorText(error);
  }
}

async function loadBays() {
  try {
    state.bays = await api("GET", "/api/bays");
  } catch {
    state.bays = { rows: 0, cols: 0, slots: [] };
  }
  $("bay-rows").value = state.bays.rows || "";
  $("bay-cols").value = state.bays.cols || "";
  renderBays();
}

document.addEventListener("DOMContentLoaded", () => {
  for (const id of ["bay-rows", "bay-cols"]) $(id).addEventListener("input", renderBayEditor);
  $("bay-edit").addEventListener("toggle", () => $("bay-edit").open && renderBayEditor());
  $("bay-save").addEventListener("click", saveBays);
});
