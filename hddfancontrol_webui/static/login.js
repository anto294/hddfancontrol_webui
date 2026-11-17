"use strict";

// The login page; while no credentials exist, it is the first setup instead:
// the code written in the log of the container, then a username and a password.
async function setupNeeded() {
  try {
    const response = await fetch("/api/auth", { headers: { "X-Requested-With": "fetch" } });
    const data = await response.json();
    return data.enabled === false;
  } catch {
    return false;
  }
}

function showSetup(form) {
  document.getElementById("setup-intro").hidden = false;
  document.getElementById("setup-code-field").hidden = false;
  document.getElementById("setup-confirm-field").hidden = false;
  form.code.required = true;
  form.confirm.required = true;
  form.password.autocomplete = "new-password";
  form.username.value = "admin";
  const button = document.getElementById("login-button");
  button.dataset.i18n = "setup.create";
  button.textContent = t("setup.create");
  form.code.focus();
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadI18n();
  const form = document.getElementById("login");
  const error = document.getElementById("login-error");
  const setup = await setupNeeded();
  if (setup) showSetup(form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    if (setup && form.password.value !== form.confirm.value) {
      error.textContent = t("error.password_mismatch");
      return;
    }
    const button = form.querySelector("button");
    button.disabled = true;
    const body = setup
      ? { code: form.code.value, username: form.username.value, password: form.password.value }
      : { username: form.username.value, password: form.password.value };
    try {
      const response = await fetch(setup ? "/api/setup" : "/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify(body),
      });
      if (response.ok) {
        location.href = "/";
        return;
      }
      const data = await response.json().catch(() => ({}));
      error.textContent = t(`error.${data.error || "unknown"}`);
    } catch {
      error.textContent = t("error.network");
    } finally {
      button.disabled = false;
    }
  });
});
