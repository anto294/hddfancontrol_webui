"use strict";

// The History tab: temperature and fan charts as inline SVG, no library.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

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
