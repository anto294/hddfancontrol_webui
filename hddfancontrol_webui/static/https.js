// FORCE_HTTPS: the server answers plain HTTP with this page. The browser goes to the
// same address over HTTPS, built from its own location, never from a header the
// server was sent.
"use strict";

(function () {
  const target = `https://${location.host}${location.pathname}${location.search}${location.hash}`;
  const link = document.getElementById("https-link");
  if (link) {
    link.href = target;
    link.textContent = target;
  }
  location.replace(target);
})();
