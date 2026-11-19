"use strict";

// The Fans tab (manual control) and the PWM test.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

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
  const reason = status.fans.safety_reason;
  $("manual-safety").textContent = reason === "stale" ? t("fans.safety_stale") : reason === "error" ? t("fans.safety_error") : t("fans.safety");
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

// A fan in use: chosen in the settings, named, or turning. The other outputs
// (empty connectors, the CPU of another board) go in a folded list.
function fanInUse(pwm, configured) {
  return configured.has(pwm.path) || Boolean(state.labels[pwm.path]) || Boolean(pwm.rpm);
}

function fanLine(pwm) {
  const range = el("input", { type: "range", min: 0, max: 255, step: 1, "aria-label": pwm.path });
  range.addEventListener("pointerdown", () => state.dragging.add(pwm.path));
  range.addEventListener("pointerup", () => state.dragging.delete(pwm.path));
  const line = el(
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
  range.addEventListener("input", () => {
    line.querySelector(".pct").textContent = fmtPct(Number(range.value));
    sendPwm(pwm.path, Number(range.value));
  });
  return line;
}

// Put nodes in a container in this order, moving only what is out of place,
// and never a line being used (moving it would drop the focus or the drag).
function placeLines(container, lines) {
  lines.forEach((line, index) => {
    if (container.children[index] === line) return;
    if (line.contains(document.activeElement) || state.dragging.has(line.dataset.path)) return;
    container.insertBefore(line, container.children[index] || null);
  });
}

function renderFans() {
  const list = $("fan-list");
  const manual = state.status ? state.status.manual : false;
  const configured = new Set((state.settings ? state.settings.pwms : []).map((pwm) => pwm.path));
  for (const device of state.hardware.hwmons) {
    if (device.pwms.length === 0) continue;
    let group = list.querySelector(`[data-hwmon="${device.hwmon}"]`);
    if (!group) {
      group = el(
        "div",
        { class: "fan-group", "data-hwmon": device.hwmon },
        el("h3", { text: `${device.name || "?"} (${device.hwmon})` }),
        el("div", { class: "fan-main" }),
        el("details", { class: "fan-others" }, el("summary"), el("p", { class: "muted small", text: t("fans.others_hint") }), el("div", { class: "fan-rest" })),
      );
      list.append(group);
    }
    const main = [];
    const rest = [];
    for (const pwm of device.pwms) {
      const line = group.querySelector(`[data-path="${CSS.escape(pwm.path)}"]`) || fanLine(pwm);
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
      (fanInUse(pwm, configured) ? main : rest).push({ pwm, line });
    }
    // The chosen fans first, each list in the order of the chip.
    main.sort((a, b) => Number(configured.has(b.pwm.path)) - Number(configured.has(a.pwm.path)));
    placeLines(group.querySelector(".fan-main"), main.map((item) => item.line));
    placeLines(group.querySelector(".fan-rest"), rest.map((item) => item.line));
    const others = group.querySelector(".fan-others");
    others.hidden = rest.length === 0;
    others.querySelector("summary").textContent = t("fans.others", { count: rest.length });
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
  const stopped = $("pwmtest-stopped");
  stopped.hidden = !test.stopped_by;
  stopped.textContent = test.stopped_by ? t(`pwmtest.stopped_${test.stopped_by}`) : "";
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
// Hardware support request: the issue form of GitHub (hardware_support.yml),
// filled in from GET /api/support. The user sees everything before sending.
// ---------------------------------------------------------------------------

const SUPPORT_FORM = "https://github.com/anto294/hddfancontrol_webui/issues/new";
// GitHub refuses longer addresses: the log, then the host report, are cut to fit.
const SUPPORT_MAX_URL = 7500;
let support = null;

function supportUrl(data) {
  const params = new URLSearchParams({ template: "hardware_support.yml", title: "[Hardware support] ", system: data.system, host: data.host, logs: data.logs });
  return `${SUPPORT_FORM}?${params}`;
}

// The report cut until its link fits: the oldest log lines go first, then the end of the host part.
function fitSupport(data) {
  const fitted = { ...data };
  let logs = fitted.logs.split("\n");
  while (supportUrl(fitted).length > SUPPORT_MAX_URL && logs.length && logs[0] !== "") {
    logs = logs.slice(1);
    fitted.logs = logs.join("\n");
  }
  while (supportUrl(fitted).length > SUPPORT_MAX_URL && fitted.host.length > 200) {
    fitted.host = `${fitted.host.slice(0, Math.floor(fitted.host.length * 0.9))}\n…`;
  }
  return { fitted, cut: fitted.logs !== data.logs || fitted.host !== data.host };
}

function supportText(data) {
  return `System: ${data.system}\n\n${data.host}\n\n== log\n${data.logs}`;
}

async function prepareSupport(fromBanner) {
  if (fromBanner) showTab("fans");
  const result = $("support-result");
  result.textContent = "";
  try {
    support = await api("GET", "/api/support");
  } catch (error) {
    $("support-preview").hidden = false;
    result.textContent = errorText(error);
    return;
  }
  // The whole report shows (and is copied); only the link may carry less.
  $("support-text").textContent = supportText(support);
  $("support-short").hidden = !fitSupport(support).cut;
  $("support-preview").hidden = false;
  $("support-card").scrollIntoView({ block: "start" });
}

function openSupport() {
  if (!support) return;
  window.open(supportUrl(fitSupport(support).fitted), "_blank", "noopener,noreferrer");
}

// The whole report, uncut. Over plain HTTP the clipboard is not available: the text is selected instead.
async function copySupport() {
  if (!support) return;
  const result = $("support-result");
  try {
    await navigator.clipboard.writeText(supportText(support));
    result.textContent = t("support.copied");
  } catch {
    const range = document.createRange();
    range.selectNodeContents($("support-text"));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    result.textContent = t("support.select");
  }
}
