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
