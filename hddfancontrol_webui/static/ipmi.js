"use strict";

// EXPERIMENTAL: the IPMI tab (bmc.py). The server model, who drives the fans
// of a Dell (the iDRAC or the web UI), the CPU profile and its curve, what
// happens now and the sensors of the BMC. Its configuration is stored apart
// from the settings (PUT /api/bmc): saving it never restarts fan control.

const ipmi = {
  // GET /api/bmc: {config, detected, vendor, models, profiles, limits}.
  data: null,
  // The configuration being edited.
  draft: null,
  // The user's own curve, kept while another profile is chosen.
  customCurve: null,
  minHint: "",
};

const IPMI_PROFILES = ["silent", "normal", "turbo", "custom"];
const IPMI_CHART_TEMPS = [20, 100];

function ipmiModel(id) {
  return ipmi.data.models.find((model) => model.id === id);
}

function ipmiModelName(model) {
  if (!model.name) return t("ipmi.model_generic");
  return model.id.endsWith("-other") ? t("ipmi.model_other", { name: model.name }) : model.name;
}

// The model in use: the one chosen, else the one found.
function ipmiCurrent() {
  return ipmiModel(ipmi.draft.model || ipmi.data.detected);
}

function ipmiMismatch() {
  const model = ipmiCurrent();
  return Boolean(model.vendor && model.vendor !== ipmi.data.vendor);
}

function ipmiCanDrive() {
  const model = ipmiCurrent();
  return model.control && ipmi.data.vendor === "dell" && !ipmiMismatch();
}

function ipmiPoints() {
  return ipmi.draft.profile === "custom" ? ipmi.draft.curve : ipmi.data.profiles[ipmi.draft.profile];
}

function curveAt(points, temp) {
  if (temp <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i++) {
    const [t1, p1] = points[i - 1];
    const [t2, p2] = points[i];
    if (temp <= t2) return p1 + ((p2 - p1) * (temp - t1)) / (t2 - t1);
  }
  return points[points.length - 1][1];
}

// The points go up in temperature and never down in speed.
function curveProblem(points) {
  for (let i = 1; i < points.length; i++) {
    if (!(points[i][0] > points[i - 1][0]) || points[i][1] < points[i - 1][1]) return t("error.curve_order");
  }
  return points.every(([temp, pct]) => Number.isFinite(temp) && Number.isFinite(pct)) ? "" : t("error.invalid_number");
}

// Who shows the fans now, in one sentence: also the overview's card.
function bmcStateText(bmc) {
  if (!bmc.can_drive) return t("bmc.read_only", { name: bmc.name });
  if (bmc.handback === "stale") return t("bmc.stale", { name: bmc.name });
  if (bmc.handback === "no_cpu_temp") return t("bmc.no_cpu_temp", { name: bmc.name });
  if (bmc.handback) return t("bmc.handback", { name: bmc.name, reason: bmc.handback });
  if (bmc.manual && bmc.percent !== null) return t("bmc.manual", { percent: bmc.percent });
  if (bmc.drives_fans && bmc.in_group) return t("bmc.waiting", { name: bmc.name });
  return t("bmc.auto", { name: bmc.name });
}

// The tab shows only with a BMC; called at every status.
function syncIpmi() {
  const bmc = state.status && state.status.bmc;
  document.querySelector('[data-tab="ipmi"]').hidden = !bmc;
  if (state.tab !== "ipmi") return;
  if (!bmc) {
    showTab("overview");
    return;
  }
  renderIpmiLive();
}

async function loadIpmi() {
  try {
    ipmi.data = await api("GET", "/api/bmc");
  } catch (error) {
    $("ipmi-result").textContent = errorText(error);
    return;
  }
  ipmi.draft = structuredClone(ipmi.data.config);
  ipmi.customCurve = structuredClone(ipmi.data.config.curve);
  ipmi.minHint = "";
  renderIpmi();
}

function radioCard(name, value, checked, title, text, onChange) {
  const radio = el("input", { type: "radio", name, value, checked });
  radio.addEventListener("change", onChange);
  return el("label", { class: "preset" }, radio, el("strong", { text: title }), el("small", { class: "muted", text }));
}

function renderIpmi() {
  if (!ipmi.data) return;
  const data = ipmi.data;
  const draft = ipmi.draft;
  const model = ipmiCurrent();

  // The server: brand, then the models of that brand; "" is the model found.
  const brand = $("ipmi-brand");
  brand.value = model.vendor || "";
  const select = $("ipmi-model");
  const options = data.models.filter((m) => (m.vendor || "") === brand.value);
  select.replaceChildren(
    ...((data.vendor || "") === brand.value ? [el("option", { value: "", text: t("ipmi.model_auto", { name: ipmiModelName(ipmiModel(data.detected)) }) })] : []),
    ...options.map((m) => el("option", { value: m.id, text: ipmiModelName(m) })),
  );
  select.value = draft.model;
  const mismatch = ipmiMismatch();
  $("ipmi-model-error").textContent = mismatch ? t("error.bmc_model_vendor", { name: state.status.bmc ? state.status.bmc.name : "BMC" }) : "";
  $("ipmi-model-note").textContent = mismatch ? "" : t(model.control ? "ipmi.note_dell" : model.vendor === "hpe" ? "ipmi.note_hpe" : "ipmi.note_other");
  $("ipmi-save").disabled = mismatch;

  // The fans: only a Dell lets the web UI drive them.
  const canDrive = ipmiCanDrive();
  $("ipmi-fans-card").hidden = !canDrive;
  if (canDrive) {
    const name = state.status && state.status.bmc ? state.status.bmc.name : "iDRAC";
    $("ipmi-control").replaceChildren(
      radioCard("ipmi-control", "bmc", draft.control === "bmc", t("ipmi.control_bmc", { name }), t("ipmi.control_bmc_text", { name }), () => setIpmi({ control: "bmc" })),
      radioCard("ipmi-control", "webui", draft.control === "webui", t("ipmi.control_webui"), t("ipmi.control_webui_text"), () => setIpmi({ control: "webui" })),
    );
    $("ipmi-webui").hidden = draft.control !== "webui";
    $("ipmi-profiles").replaceChildren(
      ...IPMI_PROFILES.map((profile) => {
        const points = profile === "custom" ? null : data.profiles[profile];
        const text = points ? t("ipmi.full_at", { temp: ltr(`${points[points.length - 1][0]} °C`) }) : t("ipmi.profile_custom_text");
        return radioCard("ipmi-profile", profile, draft.profile === profile, t(`ipmi.profile_${profile}`), text, () => chooseProfile(profile));
      }),
    );
    renderIpmiPoints();
    $("ipmi-min").value = draft.min_percent;
    $("ipmi-min").min = data.limits.min_percent[0];
    $("ipmi-min").max = data.limits.min_percent[1];
    $("ipmi-min-hint").textContent = ipmi.minHint;
    renderIpmiChart();
  }
  renderIpmiLive();
}

function setIpmi(change) {
  Object.assign(ipmi.draft, change);
  $("ipmi-result").textContent = "";
  renderIpmi();
}

function chooseProfile(profile) {
  if (profile === "custom") ipmi.draft.curve = structuredClone(ipmi.customCurve);
  setIpmi({ profile });
}

// Choosing a model applies its parameters: the minimum speed to start from.
function chooseModel(id) {
  const model = ipmiModel(id || ipmi.data.detected);
  const change = { model: id };
  if (model.control) {
    change.min_percent = model.min_percent;
    ipmi.minHint = t("ipmi.min_hint", { percent: model.min_percent, name: ipmiModelName(model) });
  }
  setIpmi(change);
}

function chooseBrand(vendor) {
  if ((ipmi.data.vendor || "") === vendor) {
    chooseModel("");
    return;
  }
  const first = ipmi.data.models.find((m) => (m.vendor || "") === vendor);
  chooseModel(first.id);
}

// The user's own points: one row each, with a button to add one more.
function renderIpmiPoints() {
  const box = $("ipmi-points");
  if (ipmi.draft.profile !== "custom") {
    box.replaceChildren();
    return;
  }
  const curve = ipmi.draft.curve;
  const [fewest, most] = ipmi.data.limits.points;
  const [low, high] = ipmi.data.limits.temps;
  const table = el("table", { class: "list form-table" }, headRow(t("ipmi.point_temp"), t("ipmi.point_percent"), ""));
  curve.forEach((point, index) => {
    const input = (which, min, max, step) => {
      const node = el("input", { type: "number", min, max, step, value: point[which], "aria-label": t(which ? "ipmi.point_percent" : "ipmi.point_temp") });
      node.addEventListener("input", () => {
        point[which] = node.value === "" ? NaN : Number(node.value);
        ipmi.customCurve = structuredClone(curve);
        $("ipmi-points-error").textContent = curveProblem(curve);
        renderIpmiChart();
      });
      return node;
    };
    const remove = el("button", { type: "button", class: "secondary", disabled: curve.length <= fewest, text: t("ipmi.remove_point") });
    remove.addEventListener("click", () => {
      curve.splice(index, 1);
      ipmi.customCurve = structuredClone(curve);
      renderIpmi();
    });
    table.append(row(input(0, low, high, 1), input(1, 0, 100, 1), remove));
  });
  const add = el("button", { type: "button", class: "secondary", disabled: curve.length >= most, text: t("ipmi.add_point") });
  add.addEventListener("click", () => {
    const [lastTemp, lastPct] = curve[curve.length - 1];
    curve.push([Math.min(high, lastTemp + 5), Math.min(100, lastPct + 10)]);
    ipmi.customCurve = structuredClone(curve);
    renderIpmi();
  });
  box.replaceChildren(el("div", { class: "table-scroll" }, table), el("small", { class: "error", id: "ipmi-points-error", text: curveProblem(curve) }), el("div", { class: "actions" }, add));
}

// The speed the CPU curve asks at each temperature, never under the minimum,
// and where the processors are now.
function renderIpmiChart() {
  const box = $("ipmi-chart");
  box.replaceChildren();
  const points = ipmiPoints();
  if (!points || curveProblem(points)) return;
  const minSpeed = Math.max(0, Math.min(100, Number($("ipmi-min").value) || 0));
  const [from, to] = IPMI_CHART_TEMPS;
  const width = Math.max(box.clientWidth, 300);
  const height = 170;
  const pad = { left: 44, right: 14, top: 10, bottom: 26 };
  const x = (temp) => pad.left + ((Math.max(from, Math.min(to, temp)) - from) / (to - from)) * (width - pad.left - pad.right);
  const y = (pct) => pad.top + (1 - pct / 100) * (height - pad.top - pad.bottom);
  const speed = (temp) => Math.max(minSpeed, curveAt(points, temp));
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height });
  for (const pct of [0, 50, 100]) {
    chart.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: y(pct), y2: y(pct), class: "gridline" }));
    chart.append(svgText({ x: pad.left - 6, y: y(pct) + 4, class: "axis", "text-anchor": "end" }, `${pct}%`));
  }
  for (const temp of niceTicks(from, to, 8)) {
    chart.append(svgText({ x: x(temp), y: height - 8, class: "axis", "text-anchor": "middle" }, `${temp}°`));
  }
  let path = "";
  for (let temp = from; temp <= to; temp += 0.5) path += `${path ? "L" : "M"}${x(temp).toFixed(1)},${y(speed(temp)).toFixed(1)}`;
  chart.append(svg("path", { d: path, class: "series c1" }));
  if (ipmi.draft.profile === "custom") {
    for (const [temp, pct] of points) chart.append(svg("circle", { cx: x(temp), cy: y(pct), r: 3, class: "point" }));
  }
  const bmc = state.status && state.status.bmc;
  if (bmc && bmc.cpu_temp !== null && bmc.cpu_temp !== undefined) {
    const dot = svg("circle", { cx: x(bmc.cpu_temp), cy: y(speed(bmc.cpu_temp)), r: 5, class: `now ${bmc.cpu_temp >= 85 ? "hot" : bmc.cpu_temp >= 70 ? "warm" : ""}`.trim() });
    const title = svg("title");
    title.textContent = t("ipmi.cpu_now", { temp: fmtTemp(bmc.cpu_temp) });
    dot.append(title);
    chart.append(dot);
  }
  box.append(chart);
}

// What happens now: who drives the fans, at what speed and why; the sensors.
function renderIpmiLive() {
  const bmc = state.status && state.status.bmc;
  if (!bmc) return;
  const facts = $("ipmi-facts");
  facts.replaceChildren();
  const fact = (box, label, value) => box.append(el("div", { class: "fact" }, el("span", { class: "muted small", text: label }), el("strong", { text: value })));
  fact(facts, "BMC", [bmc.name, bmc.firmware].filter(Boolean).join(" "));
  if (bmc.product) fact(facts, t("ipmi.product"), bmc.product);
  fact(facts, t("ipmi.fans_control"), t(bmc.can_drive ? "ipmi.fans_driveable" : "ipmi.fans_read_only"));

  $("ipmi-state").textContent = bmcStateText(bmc);
  $("ipmi-error").textContent = bmc.error ? t("bmc.error", { error: bmc.error }) : "";
  const live = $("ipmi-live");
  live.replaceChildren();
  const pct = (value) => (value === null || value === undefined ? "–" : ltr(`${value} %`));
  fact(live, t("overview.cpu_temp"), fmtTemp(bmc.cpu_temp));
  if (bmc.drives_fans) {
    fact(live, t("ipmi.cpu_asks"), pct(bmc.cpu_percent));
    fact(live, t("ipmi.drives_ask"), pct(bmc.drives_percent));
  }
  if (bmc.can_drive) fact(live, t("ipmi.fans_speed"), bmc.manual ? pct(bmc.percent) : bmc.name);
  const rpms = bmc.readings.filter((r) => r.kind === "fan" && r.value !== null).map((r) => r.value);
  if (rpms.length) fact(live, t("ipmi.fans_rpm"), fmtRpm(Math.round(rpms.reduce((a, b) => a + b, 0) / rpms.length)));

  const drives = $("ipmi-drives");
  if (bmc.in_group) drives.replaceChildren(t("ipmi.drives_group"));
  else {
    const open = el("button", { type: "button", class: "link", text: t("ipmi.drives_open") });
    open.addEventListener("click", () => showTab("settings"));
    drives.replaceChildren(t("ipmi.drives_none"), " ", open);
  }

  $("ipmi-readings").replaceChildren(
    headRow(t("ipmi.sensor"), t("ipmi.value"), t("ipmi.limit")),
    ...bmc.readings
      .filter((reading) => reading.value !== null)
      .map((reading) => {
        const value = reading.kind === "temp" ? fmtTemp(reading.value) : reading.kind === "fan" ? fmtRpm(Math.round(reading.value)) : ltr(`${Math.round(reading.value)} %`);
        const hot = reading.kind === "temp" && !["ok", "na", "ns", ""].includes(reading.status);
        return row(reading.name, el("span", { class: hot ? "error" : "", text: value }), reading.limit !== null ? fmtTemp(reading.limit) : "–");
      }),
  );
  if (ipmi.data && ipmiCanDrive() && ipmi.draft.control === "webui") renderIpmiChart();
}

async function saveIpmi() {
  const result = $("ipmi-result");
  result.textContent = "";
  $("ipmi-min-error").textContent = "";
  const draft = { ...ipmi.draft, min_percent: Number($("ipmi-min").value) };
  if (draft.profile !== "custom") draft.curve = ipmi.customCurve;
  const problem = draft.profile === "custom" ? curveProblem(draft.curve) : "";
  if (problem) {
    result.textContent = problem;
    return;
  }
  try {
    ipmi.data.config = await api("PUT", "/api/bmc", draft);
    ipmi.draft = structuredClone(ipmi.data.config);
    ipmi.minHint = "";
    result.textContent = t("settings.saved");
  } catch (error) {
    const problems = (error.data && error.data.problems) || [];
    const min = problems.find((p) => p.field === "min_percent");
    if (min) $("ipmi-min-error").textContent = t(`error.${min.code}`, min.params);
    result.textContent = problems.length ? t(`error.${problems[0].code}`, problems[0].params) : errorText(error);
    return;
  }
  renderIpmi();
  pollStatus();
}

document.addEventListener("DOMContentLoaded", () => {
  $("ipmi-brand").addEventListener("change", () => chooseBrand($("ipmi-brand").value));
  $("ipmi-model").addEventListener("change", () => chooseModel($("ipmi-model").value));
  $("ipmi-min").addEventListener("input", () => {
    ipmi.draft.min_percent = Number($("ipmi-min").value);
    renderIpmiChart();
  });
  $("ipmi-save").addEventListener("click", saveIpmi);
  $("bmc-open").addEventListener("click", () => showTab("ipmi"));
  window.addEventListener("resize", () => state.tab === "ipmi" && renderIpmiChart());
});
