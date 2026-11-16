"use strict";

// The SMART tab: one card per monitored drive, with what it is and how it is connected.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

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
  if (data.failed_past && data.failed_past.length) card.append(el("p", { class: "muted small", text: t("smart.failed_past", { attributes: data.failed_past.join(", ") }) }));
  for (const message of data.messages || []) card.append(el("p", { class: "error small", text: message }));
  const note = data.asleep ? t("smart.asleep_note") : "";
  card.append(el("p", { class: "muted small", text: `${t("smart.updated", { time: fmtTime(data.time) })} ${note}` }));
  if (data.attributes && data.attributes.length) {
    const attrs = el("table", { class: "list attrs" }, headRow("ID", t("smart.attribute"), t("smart.value"), t("smart.worst"), t("smart.thresh"), t("smart.raw")));
    for (const attr of data.attributes) {
      const line = row(String(attr.id), attr.name, String(attr.value ?? ""), String(attr.worst ?? ""), String(attr.thresh ?? ""), String(attr.raw ?? ""));
      // Data saved before failed_now existed: any when_failed counts.
      const now = attr.failed_now ?? Boolean(attr.failed);
      if (attr.failed) line.className = now ? "failed" : "failed-past";
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
