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

// How busy a drive was between the last two hardware snapshots: idle, low, mid
// or high, by the share of time with I/O under way or by the bytes moved,
// whichever is higher (an NVMe drive moves a lot in little time).
const IO_MID_BYTES = 20e6;
const IO_HIGH_BYTES = 150e6;

function ioLevel(io) {
  if (!io) return "idle";
  const bytes = io.read + io.write;
  if (io.busy < 0.01 && bytes < 4096) return "idle";
  if (io.busy >= 0.6 || bytes >= IO_HIGH_BYTES) return "high";
  return io.busy >= 0.2 || bytes >= IO_MID_BYTES ? "mid" : "low";
}

function fmtRate(bytes) {
  return bytes < 1 ? "0 B/s" : `${fmtBytes(bytes)}/s`;
}

// A hard drive seen from above with its cover off, on a 56 × 80 grid: case and
// screws, the platter with its tracks and the clamp of the spindle (they turn
// while the drive spins), and the actuator: bearing, tapered arm with the
// slider at its tip, voice coil over its magnet, flex cable, parking ramp.
function hddArt() {
  const node = svg("svg", { class: "drive-art hdd", viewBox: "0 0 56 80", "aria-hidden": "true", focusable: "false" });
  node.append(svg("rect", { class: "case", x: 1, y: 1, width: 54, height: 78, rx: 4 }));
  for (const [cx, cy] of [[4.5, 4.5], [51.5, 4.5], [4.5, 75.5], [51.5, 75.5]]) node.append(svg("circle", { class: "screw", cx, cy, r: 1.6 }));
  node.append(svg("circle", { class: "disc", cx: 28, cy: 30, r: 23 }));
  for (const r of [19, 15, 11]) node.append(svg("circle", { class: "track", cx: 28, cy: 30, r }));
  const platter = svg("g", { class: "platter" });
  platter.append(svg("path", { class: "sheen", d: "M28 9.5a20.5 20.5 0 0 1 17.8 10.3" }), svg("path", { class: "sheen", d: "M28 50.5a20.5 20.5 0 0 1-17.8-10.3" }), svg("circle", { class: "hub", cx: 28, cy: 30, r: 7 }), svg("circle", { class: "clamp", cx: 28, cy: 30, r: 4.8 }));
  for (let index = 0; index < 6; index += 1) {
    const angle = (index * Math.PI) / 3;
    platter.append(svg("circle", { class: "screw", cx: (28 + 3.3 * Math.cos(angle)).toFixed(2), cy: (30 + 3.3 * Math.sin(angle)).toFixed(2), r: 0.75 }));
  }
  platter.append(svg("circle", { class: "spindle", cx: 28, cy: 30, r: 1.4 }));
  node.append(platter);
  node.append(svg("path", { class: "ramp", d: "M8.2 47.2l4.6-1.6 0.9 6.4-4.9 2.2z" }));
  node.append(svg("path", { class: "magnet", d: "M43.5 68.5l11.5-9.5v15.5a3 3 0 0 1-3 3h-6.5z" }));
  node.append(svg("path", { class: "flex", d: "M42.5 65.5q-5 5-2 11.5" }));
  // The arm points from the bearing (46, 62) to the head (18, 38), mid-way over the platter.
  const arm = svg("g", { class: "arm" });
  arm.append(
    svg("path", { class: "coil", d: "M50.2 61.7l4.8 2.1-5.9 6.8-2.8-4.4z" }),
    svg("path", { class: "coil-gap", d: "M50.3 63.6l2.3 1-2.9 3.3-1.3-2.1z" }),
    svg("path", { class: "arm-body", d: "M47.7 60l-29.1-22.7-1.2 1.4 26.9 25.3z" }),
    svg("path", { class: "arm-hole", d: "M34 50.5l-7.6-5.3 6.4 6.7z" }),
    svg("rect", { class: "slider", x: 16.4, y: 37, width: 3.2, height: 2, rx: 0.4, transform: "rotate(40.6 18 38)" }),
    svg("circle", { class: "bearing", cx: 46, cy: 62, r: 4 }),
    svg("circle", { class: "bearing-cap", cx: 46, cy: 62, r: 1.8 }),
  );
  node.append(arm, svg("circle", { class: "led", cx: 12, cy: 75.5, r: 1.8 }));
  return node;
}

// A chip: a black package with the dot of pin 1.
function chip(x, y, width, height, extra = "") {
  const group = svg("g", { class: `pkg ${extra}`.trim() });
  group.append(svg("rect", { x, y, width, height, rx: 0.8 }), svg("circle", { class: "pin1", cx: x + 1.8, cy: y + 1.8, r: 0.6 }));
  return group;
}

// Flash drives on the same 56 × 80 grid: an M.2 stick for NVMe, an open 2.5"
// case for SATA. Both have NAND packages, a DRAM cache and a controller at
// (28, 58), and electrons that run on the traces between them while the drive
// works. Everything moves through classes (style.css), never style attributes (CSP).
function flashArt(kind) {
  const node = svg("svg", { class: `drive-art flash ${kind}`, viewBox: "0 0 56 80", "aria-hidden": "true", focusable: "false" });
  const traces = svg("path", { class: "traces" });
  if (kind === "nvme") {
    node.append(svg("rect", { class: "pcb", x: 17, y: 2, width: 22, height: 76, rx: 1.5 }), svg("circle", { class: "cut", cx: 28, cy: 2, r: 2.6 }));
    traces.setAttribute("d", "M25 22v30M31 22v30M23.5 39v3M32.5 39v3");
    node.append(traces, chip(19.5, 8, 17, 14, "nand"), chip(19.5, 25, 17, 14, "nand"), chip(21, 42, 14, 7, "dram"), chip(21, 52, 14, 12, "controller"));
    for (let x = 18.6; x < 38; x += 1.7) {
      if (x > 31.5 && x < 34) continue; // the key of an M.2 M connector
      node.append(svg("rect", { class: "gold", x: x.toFixed(1), y: 70, width: 1.1, height: 7, rx: 0.3 }));
    }
    node.append(svg("circle", { class: "led", cx: 36.8, cy: 67, r: 1.3 }));
  } else {
    node.append(svg("rect", { class: "frame", x: 3, y: 1, width: 50, height: 78, rx: 3.5 }), svg("rect", { class: "pcb", x: 7, y: 5, width: 42, height: 62, rx: 1.5 }));
    for (const [cx, cy] of [[10, 8], [46, 8], [10, 64], [46, 64]]) node.append(svg("circle", { class: "screw", cx, cy, r: 1.3 }));
    traces.setAttribute("d", "M24 52L18 39M32 52l6-13M26 52V49M30 52V49M18 22v3M38 22v3");
    node.append(traces, chip(10.5, 8, 15, 14, "nand"), chip(30.5, 8, 15, 14, "nand"), chip(10.5, 25, 15, 14, "nand"), chip(30.5, 25, 15, 14, "nand"));
    node.append(chip(21, 42, 14, 7, "dram"), chip(21, 52, 14, 12, "controller"));
    node.append(svg("rect", { class: "port", x: 12, y: 70, width: 32, height: 7, rx: 1 }));
    for (let x = 14; x < 42; x += 2) node.append(svg("rect", { class: "gold", x, y: 72, width: 1, height: 3.5 }));
    node.append(svg("circle", { class: "led", cx: 44, cy: 59, r: 1.3 }));
  }
  for (let index = 1; index <= 3; index += 1) {
    const electron = svg("g", { class: `electron e${index}` });
    electron.append(svg("circle", { class: "glow", cx: 28, cy: 58, r: 2.8 }), svg("circle", { class: "core", cx: 28, cy: 58, r: 1.2 }));
    node.append(electron);
  }
  return node;
}

function driveArt(kind) {
  return kind === "hdd" ? hddArt() : flashArt(kind);
}

// The kind of drawing for a drive: hdd, nvme, ssd, or none when it is not found.
// A drive that does not say whether it rotates is drawn as a hard drive.
function driveKind(drive) {
  if (!drive) return "none";
  if (drive.rotational !== false) return "hdd";
  return drive.bus === "nvme" || drive.name.startsWith("nvme") ? "nvme" : "ssd";
}

function bayTile(index, id, kind) {
  const tile = el("div", { "data-key": `${index}:${id}`, "data-kind": kind });
  if (kind !== "none") tile.append(driveArt(kind));
  tile.append(
    el("span", { class: "bay-number muted", text: String(index + 1) }),
    el("strong", { class: "bay-name" }),
    el("small", { class: "muted bay-kernel" }),
    el("span", { class: "bay-temp" }),
  );
  return tile;
}

// The bays as a grid; each holds the name and the temperature of its drive.
// Tiles are updated in place: building them again would restart the drawings.
function renderBays() {
  const bays = state.bays;
  const card = $("bay-card");
  const shown = bays && bays.rows * bays.cols > 0 && bays.slots.some(Boolean);
  card.hidden = !shown;
  if (!shown) return;
  const box = $("bay-map");
  box.className = `bays cols-${bays.cols}`;
  const old = new Map([...box.children].map((node) => [`${node.dataset.key}|${node.dataset.kind}`, node]));
  const tiles = bays.slots.map((id, index) => {
    if (!id) {
      return old.get(`${index}:|none`) || el("div", { class: "bay empty", "data-key": `${index}:`, "data-kind": "none", title: t("bays.empty", { number: index + 1 }) }, el("span", { class: "bay-number muted", text: String(index + 1) }), el("span", { class: "muted", text: "—" }));
    }
    const { drive, logName, temp } = bayDrive(id);
    const kind = driveKind(drive);
    const tile = old.get(`${index}:${id}|${kind}`) || bayTile(index, id, kind);
    const smart = state.smartData && state.smartData[id] ? state.smartData[id].status : "";
    const level = temp === undefined ? "gone" : temp === null ? "asleep" : tempLevel(temp, drive.name);
    const io = drive ? state.io.get(drive.name) : undefined;
    const activity = ioLevel(io);
    const className = `bay${kind === "none" ? "" : " has-art"} ${level}${smart === "failing" || smart === "warning" ? ` smart-${smart}` : ""}`;
    if (tile.className !== className) tile.className = className;
    tile.title = io ? `${id}\n${t("bays.io", { busy: Math.round(io.busy * 100), read: fmtRate(io.read), write: fmtRate(io.write) })}` : id;
    tile.querySelector(".bay-name").textContent = drive ? driveTitle(logName).name : shortPath(id);
    tile.querySelector(".bay-kernel").textContent = drive ? drive.name : "";
    tile.querySelector(".bay-temp").textContent = temp === undefined ? t("bays.missing") : temp === null ? t("state.asleep") : fmtTemp(temp);
    const art = tile.querySelector(".drive-art");
    if (art) {
      // The platters turn while the drive is awake: it has a temperature, or it works.
      const spinning = kind === "hdd" && ((temp !== null && temp !== undefined) || activity !== "idle");
      const artClass = `drive-art ${kind === "hdd" ? "hdd" : `flash ${kind}`} io-${activity}${kind === "hdd" ? (spinning ? " spinning" : " parked") : ""}`;
      if (art.getAttribute("class") !== artClass) art.setAttribute("class", artClass);
    }
    return tile;
  });
  // Moving a node out of the page and back would restart its animations too.
  if (tiles.length !== box.children.length || tiles.some((tile, index) => box.children[index] !== tile)) box.replaceChildren(...tiles);
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
