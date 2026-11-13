/*
 * DairyPro icons.
 *
 * One self-hosted icon set (Lucide, /static/icons/dp-icons.svg) for the
 * whole application, so icons look the same everywhere and keep working
 * offline. Existing markup is supported unchanged:
 *   <i data-lucide="milk"></i>
 *   <i class="fa-solid fa-trash"></i>
 *   <span class="iconify" data-icon="material-symbols:payments-outline"></span>
 * The original element is kept (its CSS and click handlers still apply) and
 * an inline <svg><use></svg> is placed inside it. Class changes made later
 * by page scripts (e.g. fa-eye -> fa-eye-slash) are followed automatically.
 */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var script = document.currentScript;
  var SPRITE = (script && script.getAttribute("data-sprite")) || "/static/icons/dp-icons.svg";

  var ALIAS = {
    "home": "house", "bar-chart-3": "chart-column", "bar-chart": "chart-bar",
    "edit": "square-pen", "edit-3": "pencil", "trash": "trash-2", "close": "x",
    "loader": "loader-circle", "alert-triangle": "triangle-alert", "help-circle": "circle-help",
    "check-circle": "circle-check", "x-circle": "circle-x", "file-chart": "file-chart-column",
    "dashboard": "layout-dashboard", "rupee": "indian-rupee", "cloud-sync": "cloud-upload"
  };
  var FA = {
    "fa-eye": "eye", "fa-eye-slash": "eye-off", "fa-arrow-left": "arrow-left",
    "fa-arrow-right": "arrow-right", "fa-lock": "lock", "fa-trash": "trash-2",
    "fa-trash-can": "trash-2", "fa-envelope": "mail", "fa-user-plus": "user-plus",
    "fa-key": "key-round", "fa-shield-halved": "shield", "fa-phone": "phone",
    "fa-clock": "clock", "fa-circle-check": "circle-check", "fa-spinner": "loader-circle",
    "fa-right-to-bracket": "log-in", "fa-cow": "milk", "fa-circle-xmark": "circle-x",
    "fa-check": "check", "fa-user-shield": "shield-check", "fa-user": "user",
    "fa-shield-keyhole": "shield", "fa-shield-check": "shield-check", "fa-save": "save",
    "fa-floppy-disk": "save", "fa-plus": "plus", "fa-pen-to-square": "square-pen",
    "fa-paper-plane": "send", "fa-edit": "square-pen", "fa-calendar-days": "calendar-days",
    "fa-calendar": "calendar", "fa-building": "building-2", "fa-ban": "ban",
    "fa-print": "printer", "fa-download": "download", "fa-search": "search",
    "fa-magnifying-glass": "search", "fa-filter": "filter", "fa-xmark": "x", "fa-times": "x",
    "fa-gear": "settings", "fa-cog": "settings", "fa-bell": "bell", "fa-info-circle": "info",
    "fa-circle-info": "info", "fa-triangle-exclamation": "triangle-alert",
    "fa-receipt": "receipt", "fa-wallet": "wallet", "fa-money-bill": "wallet",
    "fa-sign-out-alt": "log-out", "fa-right-from-bracket": "log-out", "fa-house": "house",
    "fa-users": "users", "fa-language": "languages", "fa-globe": "globe",
    "fa-rotate": "refresh-cw", "fa-sync": "refresh-cw", "fa-chevron-down": "chevron-down",
    "fa-chevron-left": "chevron-left", "fa-chevron-right": "chevron-right"
  };
  var ICONIFY = {
    "material-symbols:sell-outline": "tag", "material-symbols:bar-chart-outline": "chart-column",
    "solar:wallet-money-bold": "wallet", "solar:hand-money-bold": "hand-coins",
    "solar:file-text-bold": "file-text", "solar:cup-hot-bold": "coffee", "solar:box-bold": "box",
    "mdi:truck-outline": "truck", "material-symbols:table-chart-outline": "table",
    "material-symbols:receipt-long-outline": "receipt",
    "material-symbols:person-add-outline": "user-plus",
    "material-symbols:payments-outline": "credit-card",
    "material-symbols:local-drink-outline": "milk",
    "material-symbols:grouped-bar-chart-outline": "chart-bar",
    "material-symbols:edit-document-outline": "file-pen-line",
    "material-symbols:eco-outline": "leaf", "material-symbols:download-rounded": "download",
    "material-symbols:download-outline": "download", "material-symbols:download": "download",
    "material-symbols:calculate-outline": "calculator",
    "material-symbols:agriculture-outline": "wheat"
  };

  function nameFor(el) {
    var n = el.getAttribute("data-lucide");
    if (n) { return ALIAS[n] || n; }
    n = el.getAttribute("data-icon");
    if (n && ICONIFY[n]) { return ICONIFY[n]; }
    if (n && n.indexOf(":") === -1) { return ALIAS[n] || n; }
    var list = el.classList;
    for (var i = 0; i < list.length; i++) {
      if (FA[list[i]]) { return FA[list[i]]; }
    }
    return null;
  }

  function render(el) {
    var name = nameFor(el);
    if (!name) { return; }
    if (el.getAttribute("data-dp-icon") === name && el.querySelector(":scope > svg.dp-icon")) {
      return;
    }
    var svg = el.querySelector(":scope > svg.dp-icon");
    if (!svg) {
      svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "dp-icon");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      svg.appendChild(document.createElementNS(NS, "use"));
      el.textContent = "";
      el.appendChild(svg);
    }
    svg.firstChild.setAttribute("href", SPRITE + "#" + name);
    el.setAttribute("data-dp-icon", name);
    if (!el.hasAttribute("aria-label") && !el.hasAttribute("aria-hidden")) {
      el.setAttribute("aria-hidden", "true");
    }
  }

  var SELECTOR = "[data-lucide], .iconify[data-icon], [data-icon].dp-i, i[class*='fa-']";

  function scan(root) {
    if (!root || !root.querySelectorAll) { return; }
    if (root.matches && root.matches(SELECTOR)) { render(root); }
    root.querySelectorAll(SELECTOR).forEach(render);
  }

  // Helper for scripts that build buttons dynamically.
  function icon(name, extraClass) {
    var i = document.createElement("i");
    i.setAttribute("data-lucide", name);
    if (extraClass) { i.className = extraClass; }
    render(i);
    return i;
  }

  function start() {
    scan(document);
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        if (m.type === "attributes") { render(m.target); return; }
        m.addedNodes.forEach(function (node) { if (node.nodeType === 1) { scan(node); } });
      });
    }).observe(document.documentElement, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ["class", "data-lucide", "data-icon"]
    });
  }

  window.DPIcons = { scan: scan, icon: icon, sprite: SPRITE };
  // Compatibility with existing calls: lucide.createIcons()
  window.lucide = window.lucide || { createIcons: function () { scan(document); } };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
