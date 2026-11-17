"use strict";

// The History tab: temperature and fan charts as inline SVG, no library.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

// ---------------------------------------------------------------------------
// History charts (inline SVG, no library)
// ---------------------------------------------------------------------------

const SVG = "http://www.w3.org/2000/svg";

// Series switched on or off in a legend, by "kind:name"; others follow their default.
const seriesShown = new Map();

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function svgText(attrs, text) {
  const node = svg("text", attrs);
  node.textContent = text;
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

// The point of a series nearest to ts, if it is within three steps.
function nearest(points, ts, step) {
  let low = 0;
  let high = points.length - 1;
  while (high - low > 1) {
    const mid = (low + high) >> 1;
    if (points[mid][0] < ts) low = mid;
    else high = mid;
  }
  const best = [points[low], points[high]].filter(Boolean).sort((a, b) => Math.abs(a[0] - ts) - Math.abs(b[0] - ts))[0];
  return best && Math.abs(best[0] - ts) <= 3 * step ? best : null;
}

// The median time between two points of a series.
function usualStep(points) {
  const steps = [];
  for (let i = 1; i < points.length; i += 1) steps.push(points[i][0] - points[i - 1][0]);
  if (!steps.length) return 0;
  steps.sort((a, b) => a - b);
  return steps[steps.length >> 1];
}

function fmtValue(value, unit) {
  return `${Number.isInteger(value) ? value : value.toFixed(1)}${unit}`;
}

// A drive of the history: a /dev/disk/by-id path, or (before 0.2.0 and for
// drives the host does not list) the "sda MODEL" name of hddfancontrol.
function seriesDriveName(name) {
  if (!name.startsWith("/dev/")) {
    const title = driveTitle(name);
    return `${title.name} · ${name.split(" ")[0]}`;
  }
  const drive = state.hardware.drives.find((d) => d.ids.includes(name));
  const label = driveLabelById(name);
  const main = label || shortModel(drive ? drive.model : "") || shortPath(name);
  return drive ? `${main} · ${drive.name}` : main;
}

// Fans that are not chosen, have no name and never turned are hidden until
// switched on in the legend: unused connectors only add flat lines.
function fanHiddenByDefault(path, rpmSeries) {
  const chosen = (state.settings ? state.settings.pwms : []).some((pwm) => pwm.path === path);
  if (chosen || state.labels[path]) return false;
  const rpm = rpmSeries.find((s) => s.name === path);
  return !rpm || rpm.points.every(([, value]) => !value);
}

// options: unit, labelOf(name), kind, refs [{value, label, class}], hiddenByDefault(name).
function drawChart(container, data, series, options) {
  container.replaceChildren();
  if (!series.length) {
    container.append(el("p", { class: "muted", text: t("history.empty") }));
    return;
  }
  const { unit, labelOf, kind } = options;
  const refs = options.refs || [];
  const key = (s) => `${kind}:${s.name}`;
  const shown = (s) => (seriesShown.has(key(s)) ? seriesShown.get(key(s)) : !(options.hiddenByDefault && options.hiddenByDefault(s.name)));
  // Colors follow the place in the full list, so they do not move when a series is hidden.
  const colored = series.map((s, index) => ({
    ...s,
    color: `c${(index % 8) + 1}`,
    label: labelOf(s.name),
    step: Math.max(data.bucket, usualStep(s.points)),
  }));
  const visible = colored.filter(shown);

  const width = Math.max(container.clientWidth, 300);
  const height = 240;
  const pad = { left: 52, right: 12, top: 10, bottom: 28 };
  let min = Infinity;
  let max = -Infinity;
  for (const s of visible) {
    for (const [, value] of s.points) {
      min = Math.min(min, value);
      max = Math.max(max, value);
    }
  }
  if (min === Infinity) {
    min = 0;
    max = 1;
  }
  // A reference line counts when it is near the data, so that it does not flatten the lines.
  for (const ref of refs) {
    if (ref.value >= min - 10 && ref.value <= max + 10) {
      min = Math.min(min, ref.value);
      max = Math.max(max, ref.value);
    }
  }
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const margin = (max - min) * 0.08;
  min -= margin;
  max += margin;
  const plotRight = width - pad.right;
  const x = (ts) => pad.left + ((ts - data.start) / (data.end - data.start)) * (plotRight - pad.left);
  const y = (value) => pad.top + (1 - (value - min) / (max - min)) * (height - pad.top - pad.bottom);

  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": options.title || "" });
  for (const tick of niceTicks(min, max, 4)) {
    chart.append(svg("line", { x1: pad.left, x2: plotRight, y1: y(tick), y2: y(tick), class: "gridline" }));
    chart.append(svgText({ x: pad.left - 6, y: y(tick) + 4, class: "axis", "text-anchor": "end" }, `${tick}${unit}`));
  }
  const long = data.end - data.start > 2 * 86400;
  const timeText = (ts, withDate) => {
    const date = new Date(ts * 1000);
    const time = date.toLocaleTimeString(I18N.lang, { hour: "2-digit", minute: "2-digit" });
    return withDate ? `${date.toLocaleDateString(I18N.lang, { day: "2-digit", month: "2-digit" })} ${time}` : time;
  };
  for (let i = 0; i <= 4; i += 1) {
    const ts = data.start + ((data.end - data.start) * i) / 4;
    const date = new Date(ts * 1000);
    const text = long ? date.toLocaleDateString(I18N.lang, { day: "2-digit", month: "2-digit" }) : timeText(ts, false);
    chart.append(svgText({ x: x(ts), y: height - 8, class: "axis", "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, text));
  }
  for (const ref of refs) {
    if (ref.value < min || ref.value > max) continue;
    chart.append(svg("line", { x1: pad.left, x2: plotRight, y1: y(ref.value), y2: y(ref.value), class: `ref ${ref.class}` }));
    chart.append(svgText({ x: plotRight - 4, y: y(ref.value) - 4, class: `ref-label ${ref.class}`, "text-anchor": "end" }, `${ref.label} ${fmtValue(ref.value, unit)}`));
  }
  for (const s of visible) {
    // A gap (a sleeping drive, a stopped container) breaks the line: more
    // than three buckets, or three usual steps of the series when its
    // samples are further apart than a bucket (short windows).
    const gap = 3 * s.step;
    let path = "";
    let last = null;
    for (const [ts, value] of s.points) {
      const move = last === null || ts - last > gap;
      path += `${move ? "M" : "L"}${x(ts).toFixed(1)},${y(value).toFixed(1)}`;
      last = ts;
    }
    // A class, not a style attribute: the CSP blocks inline styles.
    chart.append(svg("path", { d: path, class: `series ${s.color}` }));
  }

  // Hover (or touch): a line at the time under the pointer and the value of each series then.
  const cursor = svg("g", { class: "cursor", visibility: "hidden" });
  chart.append(cursor);
  const hit = svg("rect", { x: pad.left, y: pad.top, width: plotRight - pad.left, height: height - pad.top - pad.bottom, class: "hit" });
  chart.append(hit);
  const showAt = (event) => {
    const box = chart.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * width;
    const ts = data.start + ((Math.min(Math.max(px, pad.left), plotRight) - pad.left) / (plotRight - pad.left)) * (data.end - data.start);
    const rows = visible
      .map((s) => ({ s, point: nearest(s.points, ts, s.step) }))
      .filter((r) => r.point)
      .sort((a, b) => b.point[1] - a.point[1]);
    cursor.replaceChildren();
    if (!rows.length) {
      cursor.setAttribute("visibility", "hidden");
      return;
    }
    const at = x(rows[0].point[0]);
    cursor.append(svg("line", { x1: at, x2: at, y1: pad.top, y2: height - pad.bottom, class: "cursor-line" }));
    for (const { s, point } of rows) cursor.append(svg("circle", { cx: x(point[0]), cy: y(point[1]), r: 3, class: `dot ${s.color}` }));
    const lineHeight = 15;
    const lines = rows.slice(0, 12);
    const boxWidth = Math.min(260, Math.max(...lines.map(({ s }) => s.label.length)) * 6.2 + 70);
    const boxHeight = (lines.length + 1) * lineHeight + 10;
    const left = at + 12 + boxWidth > plotRight ? at - 12 - boxWidth : at + 12;
    const top = pad.top + 4;
    cursor.append(svg("rect", { x: left, y: top, width: boxWidth, height: boxHeight, rx: 6, class: "tip" }));
    cursor.append(svgText({ x: left + 8, y: top + 15, class: "tip-time" }, timeText(rows[0].point[0], long)));
    lines.forEach(({ s, point }, index) => {
      const ty = top + 15 + (index + 1) * lineHeight;
      cursor.append(svg("rect", { x: left + 8, y: ty - 8, width: 10, height: 3, rx: 1.5, class: `swatch ${s.color}` }));
      const name = s.label.length > 30 ? `${s.label.slice(0, 29)}…` : s.label;
      cursor.append(svgText({ x: left + 24, y: ty, class: "tip-text" }, name));
      cursor.append(svgText({ x: left + boxWidth - 8, y: ty, class: "tip-value", "text-anchor": "end" }, fmtValue(point[1], unit)));
    });
    cursor.setAttribute("visibility", "visible");
  };
  hit.addEventListener("pointermove", showAt);
  hit.addEventListener("pointerdown", showAt);
  hit.addEventListener("pointerleave", () => cursor.setAttribute("visibility", "hidden"));

  // The legend switches series on and off.
  const legend = el("div", { class: "legend" });
  for (const s of colored) {
    const on = shown(s);
    const lastValue = s.points.length ? s.points[s.points.length - 1][1] : null;
    legend.append(
      el(
        "button",
        {
          type: "button",
          class: `key${on ? "" : " off"}`,
          "aria-pressed": String(on),
          title: on ? t("history.hide_series") : t("history.show_series"),
          onclick: () => {
            seriesShown.set(key(s), !on);
            drawChart(container, data, series, options);
          },
        },
        el("i", { class: s.color }),
        `${s.label}${lastValue === null ? "" : ` · ${fmtValue(lastValue, unit)}`}`,
      ),
    );
  }
  container.append(chart, legend);
}

async function loadHistory() {
  const hours = $("history-hours").value;
  // The CSV covers the window chosen.
  $("history-csv").setAttribute("href", `/api/history.csv?hours=${encodeURIComponent(hours)}`);
  try {
    const data = await api("GET", `/api/history?hours=${encodeURIComponent(hours)}`);
    const of = (kind) => data.series.filter((s) => s.kind === kind);
    const settings = state.settings;
    const refs = [];
    if (settings) {
      refs.push({ value: settings.drive_temp_max, label: t("history.ref_max"), class: "max" });
      if (settings.control_mode === "auto") refs.push({ value: settings.target_temp, label: t("history.ref_target"), class: "target" });
      else refs.push({ value: settings.drive_temp_min, label: t("history.ref_min"), class: "min" });
    }
    const hidden = (name) => fanHiddenByDefault(name, of("rpm"));
    drawChart($("chart-temp"), data, of("temp"), { unit: "°", labelOf: seriesDriveName, kind: "temp", refs, title: t("history.temps") });
    drawChart($("chart-rpm"), data, of("rpm"), { unit: "", labelOf: pwmLabel, kind: "rpm", hiddenByDefault: hidden, title: t("history.rpm") });
    drawChart($("chart-pwm"), data, of("pwm"), { unit: "%", labelOf: pwmLabel, kind: "pwm", hiddenByDefault: hidden, title: t("history.pwm") });
  } catch (error) {
    alertBanner(errorText(error));
  }
}
