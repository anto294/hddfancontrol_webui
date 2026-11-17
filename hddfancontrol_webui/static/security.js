"use strict";

// The Security tab (username and password) and the Logs tab.
// Loaded after app.js, whose helpers and state it uses; see app.js for the rules.

// ---------------------------------------------------------------------------
// Security
// ---------------------------------------------------------------------------

async function loadAuth() {
  try {
    const auth = await api("GET", "/api/auth");
    const form = $("auth-form");
    $("security-state").textContent = t(`security.state_${auth.source}`, { username: auth.username });
    form.hidden = auth.source === "environment";
    $("current-field").hidden = !auth.enabled;
    form.username.value = auth.username || "admin";
  } catch (error) {
    alertBanner(errorText(error));
  }
}

async function saveCredentials(event) {
  event.preventDefault();
  const form = $("auth-form");
  const result = $("auth-result");
  result.className = "small error";
  if (form.password.value !== form.confirm.value) {
    result.textContent = t("error.password_mismatch");
    return;
  }
  try {
    await api("PUT", "/api/auth", { username: form.username.value, password: form.password.value, current: form.current.value });
    form.reset();
    result.textContent = t("security.saved");
    result.className = "small ok";
  } catch (error) {
    result.textContent = errorText(error);
  }
  loadAuth();
  pollStatus();
}

async function logout() {
  try {
    await api("POST", "/api/logout");
  } finally {
    location.href = "/login";
  }
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

function formatLog(line) {
  const time = new Date(line.time).toLocaleTimeString(I18N.lang);
  return `${time} [${line.source}] ${line.text}\n`;
}

function renderLogs() {
  const source = $("log-source").value;
  const lines = state.logLines.filter((line) => !source || line.source === source);
  $("log").textContent = lines.map(formatLog).join("");
  $("log").scrollTop = $("log").scrollHeight;
}

async function pollLogs() {
  try {
    const data = await api("GET", `/api/logs?after=${state.logSeq}`);
    if (!data.lines.length) return;
    state.logSeq = data.last;
    state.logLines.push(...data.lines);
    const trimmed = state.logLines.length > LOG_MAX_LINES;
    if (trimmed) state.logLines.splice(0, state.logLines.length - LOG_MAX_LINES);
    if (state.tab !== "logs") return;
    // Appending only would let the page keep every line ever shown.
    if (trimmed) {
      renderLogs();
      return;
    }
    const log = $("log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const source = $("log-source").value;
    log.append(data.lines.filter((line) => !source || line.source === source).map(formatLog).join(""));
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch {
    // The next poll tries again.
  }
}
