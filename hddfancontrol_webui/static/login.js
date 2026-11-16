"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  await loadI18n();
  const form = document.getElementById("login");
  const error = document.getElementById("login-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify({ username: form.username.value, password: form.password.value }),
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
