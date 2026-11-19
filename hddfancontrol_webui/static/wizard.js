"use strict";

// The guided setup: one question per step, in plain words, for someone who
// has never set up fan control. It only fills a settings document and saves
// it through the same API as the Settings tab; everything it does can be
// changed there afterwards. Uses the helpers of app.js (el, api, t, ...).

const WIZARD_SKIP_KEY = "setupSkipped";
const IDENTIFY_SECONDS = 10;
// Used when the PWM test is skipped: a fan that stops below 110 is rare, and
// with the minimum speed of 20 % hddfancontrol never goes under about 140.
const SAFE_START = 200;
const SAFE_STOP = 110;
const PRESETS = {
  quiet: { min: 40, max: 52 },
  balanced: { min: 35, max: 50 },
  cool: { min: 30, max: 45 },
};
const SPIN_DOWN = { never: 0, short: 1200, long: 3600 };
const STEPS = ["welcome", "drives", "fans", "measure", "temps", "spindown", "summary"];

const wizard = {
  step: 0,
  drives: new Set(),
  pwms: new Set(),
  measured: {},
  preset: "balanced",
  spinDown: "never",
  identifying: null,
  timer: null,
};

// The first stable name of a drive, as the Settings tab picks it.
function wizardDriveId(drive) {
  return drive.ids[0];
}

// First start: nothing chosen yet and nothing saved from the web UI.
function wizardWanted() {
  if (!state.settings || state.settingsSource !== "environment") return false;
  if (state.settings.drives.length || state.settings.pwms.length) return false;
  try {
    return localStorage.getItem(WIZARD_SKIP_KEY) !== "1";
  } catch {
    return true;
  }
}

function openWizard() {
  wizard.step = 0;
  wizard.drives = new Set(state.hardware.drives.filter((d) => d.rotational !== false).map(wizardDriveId));
  wizard.pwms = new Set();
  wizard.measured = {};
  showTab("setup");
  renderBanners();
  renderWizard();
}

function closeWizard() {
  try {
    localStorage.setItem(WIZARD_SKIP_KEY, "1");
  } catch {
    // Then the guided setup may open again at the next visit; no harm.
  }
  stopIdentify();
  showTab("overview");
  renderBanners();
}

function wizardNav(canGoOn, nextLabel = t("wizard.next"), onNext = () => goTo(wizard.step + 1)) {
  const back = el("button", { type: "button", class: "secondary", "data-icon": "arrow_left", text: t("wizard.back"), onclick: () => goTo(wizard.step - 1) });
  back.hidden = wizard.step === 0;
  const next = el("button", { type: "button", "data-icon": "arrow_right", text: nextLabel, disabled: !canGoOn, onclick: onNext });
  const skip = el("button", { type: "button", class: "link", text: t("wizard.close"), onclick: closeWizard });
  const nav = el("div", { class: "actions wizard-nav" }, back, next, skip);
  applyIcons(nav);
  return nav;
}

function goTo(step) {
  stopIdentify();
  wizard.step = Math.max(0, Math.min(STEPS.length - 1, step));
  renderWizard();
  window.scrollTo(0, 0);
}

function renderWizard() {
  const name = STEPS[wizard.step];
  const progress = el(
    "ol",
    { class: "wizard-steps", "aria-label": t("wizard.progress") },
    ...STEPS.map((step, index) =>
      el("li", { class: index < wizard.step ? "done" : index === wizard.step ? "current" : "", "aria-current": index === wizard.step ? "step" : null }, el("span", { text: String(index + 1) })),
    ),
  );
  const body = el("div", { class: "wizard-body" });
  const steps = { welcome: stepWelcome, drives: stepDrives, fans: stepFans, measure: stepMeasure, temps: stepTemps, spindown: stepSpinDown, summary: stepSummary };
  steps[name](body);
  $("setup-progress").replaceChildren(progress);
  $("setup-body").replaceChildren(body);
}

function stepTitle(iconName, key) {
  return el("h2", { "data-icon": iconName, text: t(key) });
}

function finish(body) {
  applyIcons(body);
}

// --- welcome ---------------------------------------------------------------

function stepWelcome(body) {
  body.append(stepTitle("wizard", "wizard.welcome_title"), el("p", { text: t("wizard.welcome_text") }));
  const list = el("ul", { class: "wizard-list" });
  for (const key of ["wizard.welcome_1", "wizard.welcome_2", "wizard.welcome_3", "wizard.welcome_4"]) list.append(el("li", { text: t(key) }));
  body.append(list);
  if (state.status && state.status.read_only) {
    body.append(banner("error", t("wizard.read_only")), wizardNav(false));
  } else if (!allPwms().length) {
    body.append(banner("warning", t("wizard.no_pwm")), wizardNav(false));
  } else {
    body.append(el("p", { class: "muted small", text: t("wizard.welcome_safe") }), wizardNav(true, t("wizard.start")));
  }
  finish(body);
}

// --- drives ----------------------------------------------------------------

function stepDrives(body) {
  body.append(stepTitle("drive", "wizard.drives_title"), el("p", { text: t("wizard.drives_text") }));
  const list = el("div", { class: "choice-list" });
  for (const drive of state.hardware.drives) {
    const id = wizardDriveId(drive);
    const label = driveLabelById(id);
    const kind = drive.rotational === false ? t("wizard.ssd") : t("wizard.hdd");
    const box = el("input", { type: "checkbox", checked: wizard.drives.has(id) });
    box.addEventListener("change", () => {
      if (box.checked) wizard.drives.add(id);
      else wizard.drives.delete(id);
      next.disabled = wizard.drives.size === 0;
    });
    list.append(
      el(
        "label",
        { class: "choice" },
        box,
        icon("drive"),
        el("span", { class: "choice-text" }, el("strong", { text: label || `${drive.model || drive.name}` }), el("small", { class: "muted", text: `${drive.name} · ${fmtBytes(drive.size_bytes)} · ${kind}` })),
      ),
    );
  }
  if (!state.hardware.drives.length) list.append(el("p", { class: "muted", text: t("wizard.no_drives") }));
  body.append(list, el("p", { class: "muted small", text: t("wizard.drives_hint") }));
  const nav = wizardNav(wizard.drives.size > 0);
  const next = nav.querySelector("button:not(.secondary):not(.link)");
  body.append(nav);
  finish(body);
}

// --- fans ------------------------------------------------------------------

async function identify(path) {
  stopIdentify();
  try {
    if (!state.status.manual) await api("PUT", "/api/manual", { enabled: true });
    await api("PUT", "/api/manual/pwm", { path, value: 255 });
  } catch (error) {
    alertBanner(errorText(error));
    return;
  }
  wizard.identifying = { path, left: IDENTIFY_SECONDS };
  renderWizard();
  wizard.timer = setInterval(async () => {
    if (!wizard.identifying) return;
    wizard.identifying.left -= 1;
    if (wizard.identifying.left <= 0) {
      stopIdentify();
      renderWizard();
      return;
    }
    await pollHardware();
    updateFanChoices();
  }, 1000);
}

// Only the numbers change while a fan runs: building the step again would
// throw away a name being typed.
function updateFanChoices() {
  if (state.tab !== "setup" || STEPS[wizard.step] !== "fans") return;
  for (const pwm of allPwms()) {
    const line = document.querySelector(`#setup-body .choice[data-path="${CSS.escape(pwm.path)}"]`);
    if (!line) continue;
    line.querySelector(".reading").textContent = `${fmtPct(pwm.value)} · ${fmtRpm(pwm.rpm)}`;
    const busy = wizard.identifying && wizard.identifying.path === pwm.path;
    if (busy) line.querySelector("button").lastChild.textContent = t("wizard.identify_running", { seconds: wizard.identifying.left });
  }
}

// Leaving manual control gives every output back the setting it had.
function stopIdentify() {
  if (wizard.timer === null) return;
  clearInterval(wizard.timer);
  wizard.timer = null;
  wizard.identifying = null;
  api("PUT", "/api/manual", { enabled: false })
    .catch((error) => alertBanner(errorText(error)))
    .finally(pollStatus);
}

function stepFans(body) {
  body.append(stepTitle("fan", "wizard.fans_title"), el("p", { text: t("wizard.fans_text") }), banner("warning", t("wizard.fans_cpu")));
  const list = el("div", { class: "choice-list" });
  for (const pwm of allPwms()) {
    const busy = wizard.identifying && wizard.identifying.path === pwm.path;
    const box = el("input", { type: "checkbox", checked: wizard.pwms.has(pwm.path) });
    box.addEventListener("change", () => {
      if (box.checked) wizard.pwms.add(pwm.path);
      else wizard.pwms.delete(pwm.path);
      next.disabled = wizard.pwms.size === 0;
    });
    const name = labelInput(pwm.path, state.labels[pwm.path]);
    const button = el("button", {
      type: "button",
      class: busy ? "danger" : "secondary",
      "data-icon": "fan",
      text: busy ? t("wizard.identify_running", { seconds: wizard.identifying.left }) : t("wizard.identify"),
      disabled: Boolean(wizard.identifying) && !busy,
      onclick: () => (busy ? (stopIdentify(), renderWizard()) : identify(pwm.path)),
    });
    list.append(
      el(
        "div",
        { class: `choice${busy ? " active" : ""}`, "data-path": pwm.path },
        box,
        icon("fan", busy ? "spin-fast" : spinClass(pwm)),
        el("span", { class: "choice-text" }, el("strong", { text: pwmLabel(pwm.path) }), el("small", { class: "muted reading", text: `${fmtPct(pwm.value)} · ${fmtRpm(pwm.rpm)}` }), name),
        button,
      ),
    );
  }
  body.append(list, el("p", { class: "muted small", text: t("wizard.fans_hint") }));
  const nav = wizardNav(wizard.pwms.size > 0);
  const next = nav.querySelector("button:not(.secondary):not(.link)");
  body.append(nav);
  finish(body);
}

// --- measure (PWM test) ----------------------------------------------------

async function startMeasure() {
  try {
    await api("POST", "/api/pwm-test", { paths: [...wizard.pwms].filter((path) => path !== BMC_PWM) });
  } catch (error) {
    alertBanner(errorText(error));
  }
  await pollStatus();
  renderWizard();
}

// Called by pollStatus: follows the PWM test while this step is shown.
function wizardOnStatus() {
  if (state.tab !== "setup" || STEPS[wizard.step] !== "measure") return;
  const test = state.status.pwm_test;
  for (const [path, result] of Object.entries(test.results)) {
    if (result.status === "done" && wizard.pwms.has(path)) wizard.measured[path] = { start: result.start, stop: result.stop };
  }
  renderWizard();
}

function stepMeasure(body) {
  body.append(stepTitle("pwmtest", "wizard.measure_title"), el("p", { text: t("wizard.measure_text") }));
  const test = state.status.pwm_test;
  const table = el("table", { class: "list" }, headRow(t("fans.output"), t("pwmtest.status"), t("settings.start"), t("settings.stop")));
  for (const path of wizard.pwms) {
    const result = test.paths.includes(path) ? test.results[path] || { status: "pending" } : null;
    const measured = wizard.measured[path];
    const status = measured ? t("pwmtest.done") : result ? t(`pwmtest.${result.status}`) + (test.current === path ? " …" : "") : t("wizard.not_measured");
    if (path === BMC_PWM) table.append(row(pwmLabel(path), "—", String(BMC_START_STOP), String(BMC_START_STOP)));
    else table.append(row(pwmLabel(path), status, String(measured ? measured.start : SAFE_START), String(measured ? measured.stop : SAFE_STOP)));
  }
  body.append(table);
  const all = [...wizard.pwms].every((path) => wizard.measured[path] || path === BMC_PWM);
  if (test.running) {
    body.append(
      el("p", { class: "warning", text: t("wizard.measure_running") }),
      el("div", { class: "actions" }, el("button", { type: "button", class: "danger", text: t("action.cancel_test"), onclick: cancelPwmTest })),
    );
  } else if (!all) {
    body.append(
      el("p", { class: "muted small", text: t("wizard.measure_skip", { start: SAFE_START, stop: SAFE_STOP }) }),
      el("div", { class: "actions" }, el("button", { type: "button", "data-icon": "pwmtest", text: t("wizard.measure_start"), onclick: startMeasure })),
    );
  }
  const nav = wizardNav(!test.running, all ? t("wizard.next") : t("wizard.measure_later"));
  // Until the fans are measured, starting the test is the main choice.
  if (!all) nav.querySelector("button:not(.secondary):not(.link)").className = "secondary";
  body.append(nav);
  finish(body);
}

// --- temperatures ----------------------------------------------------------

function stepTemps(body) {
  body.append(stepTitle("thermometer", "wizard.temps_title"), el("p", { text: t("wizard.temps_text") }));
  const list = el("div", { class: "preset-list", role: "radiogroup" });
  for (const [name, preset] of Object.entries(PRESETS)) {
    const radio = el("input", { type: "radio", name: "preset", value: name, checked: wizard.preset === name });
    radio.addEventListener("change", () => (wizard.preset = name));
    list.append(
      el(
        "label",
        { class: "preset" },
        radio,
        el("strong", { text: t(`wizard.preset_${name}`) }),
        el("span", { class: "preset-range", text: ltr(`${preset.min} – ${preset.max} °C`) }),
        el("small", { class: "muted", text: t(`wizard.preset_${name}_text`, { min: preset.min, max: preset.max }) }),
      ),
    );
  }
  body.append(list, el("p", { class: "muted small", text: t("wizard.temps_hint") }), wizardNav(true));
  finish(body);
}

// --- spin down -------------------------------------------------------------

function stepSpinDown(body) {
  body.append(stepTitle("moon", "wizard.spindown_title"), el("p", { text: t("wizard.spindown_text") }));
  const list = el("div", { class: "preset-list", role: "radiogroup" });
  for (const name of Object.keys(SPIN_DOWN)) {
    const radio = el("input", { type: "radio", name: "spindown", value: name, checked: wizard.spinDown === name });
    radio.addEventListener("change", () => (wizard.spinDown = name));
    list.append(el("label", { class: "preset" }, radio, el("strong", { text: t(`wizard.spindown_${name}`) }), el("small", { class: "muted", text: t(`wizard.spindown_${name}_text`) })));
  }
  body.append(list, wizardNav(true));
  finish(body);
}

// --- summary and save ------------------------------------------------------

function wizardDocument() {
  const preset = PRESETS[wizard.preset];
  return {
    ...state.settings,
    control_mode: "curve",
    drive_temp_min: preset.min,
    drive_temp_max: preset.max,
    drives: [...wizard.drives],
    monitor_drives: [...wizard.drives],
    pwms: [...wizard.pwms].map((path) =>
      path === BMC_PWM
        ? { path, start: BMC_START_STOP, stop: BMC_START_STOP }
        : { path, start: wizard.measured[path] ? wizard.measured[path].start : SAFE_START, stop: wizard.measured[path] ? wizard.measured[path].stop : SAFE_STOP },
    ),
    spin_down_time_s: SPIN_DOWN[wizard.spinDown],
    spin_down_drives: {},
  };
}

function stepSummary(body) {
  const preset = PRESETS[wizard.preset];
  body.append(stepTitle("check", "wizard.summary_title"), el("p", { text: t("wizard.summary_text") }));
  const table = el("table", { class: "list" });
  const drives = state.hardware.drives.filter((d) => wizard.drives.has(wizardDriveId(d)));
  table.append(row(t("wizard.summary_drives"), drives.map((d) => driveLabelById(wizardDriveId(d)) || `${d.model} (${d.name})`).join(", ")));
  table.append(row(t("wizard.summary_fans"), [...wizard.pwms].map(pwmLabel).join(", ")));
  table.append(row(t("wizard.summary_temps"), `${t(`wizard.preset_${wizard.preset}`)} · ${ltr(`${preset.min} – ${preset.max} °C`)}`));
  table.append(row(t("wizard.summary_spindown"), t(`wizard.spindown_${wizard.spinDown}`)));
  const result = el("p", { class: "error" });
  body.append(table, result, wizardNav(true, t("wizard.save"), () => saveWizard(result)));
  finish(body);
}

async function saveWizard(result) {
  result.textContent = "";
  try {
    const data = await api("PUT", "/api/settings", wizardDocument());
    state.settings = data.settings;
    state.settingsSource = data.source;
  } catch (error) {
    const problems = error.data && error.data.problems ? error.data.problems.map((p) => `${p.field}: ${t(`error.${p.code}`, p.params || {})}`) : [];
    result.textContent = [errorText(error), ...problems].join(" ");
    return;
  }
  closeWizard();
  await pollStatus();
  renderSettings();
}
