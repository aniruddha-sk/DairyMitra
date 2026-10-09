/*
 * DairyMitr CSRF helper.
 * - Adds the X-CSRFToken header to same-origin fetch() requests that change
 *   data (POST/PUT/PATCH/DELETE).
 * - Adds a csrf_token field to any POST form that lacks one (safety net;
 *   templates already render the field server-side).
 */
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta || !window.fetch) { return; }
  var token = meta.getAttribute("content");

  function sameOrigin(url) {
    if (!url) { return true; }
    try { return new URL(url, window.location.href).origin === window.location.origin; }
    catch (e) { return false; }
  }

  var originalFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === "string" ? input : (input && input.url);
    var method = (init.method || (input && input.method) || "GET").toUpperCase();
    if (sameOrigin(url) && !/^(GET|HEAD|OPTIONS)$/.test(method)) {
      var headers = new Headers(init.headers || (input && input.headers) || {});
      if (!headers.has("X-CSRFToken")) { headers.set("X-CSRFToken", token); }
      init.headers = headers;
      if (!init.credentials) { init.credentials = "same-origin"; }
    }
    return originalFetch.call(this, input, init);
  };

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || (form.getAttribute("method") || "").toLowerCase() !== "post") { return; }
    if (form.querySelector('input[name="csrf_token"]')) { return; }
    var field = document.createElement("input");
    field.type = "hidden";
    field.name = "csrf_token";
    field.value = token;
    form.appendChild(field);
  }, true);
})();
