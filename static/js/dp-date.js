/*
 * DairyPro date fields: always DD/MM/YYYY on screen.
 *
 * Browsers draw <input type="date"> in the phone/PC locale (often
 * MM/DD/YYYY). Every date input is therefore paired with a DD/MM/YYYY text
 * field and a calendar button. The ORIGINAL input stays in the form (same
 * id, name and events) and still submits ISO YYYY-MM-DD, so the server and
 * page scripts are unaffected and day/month can never be swapped.
 *
 * Opt out per field with data-native-date.
 */
(function () {
  "use strict";
  var proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  var t = window.t || function (s) { return s; };

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function isoToDmy(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? m[3] + "/" + m[2] + "/" + m[1] : "";
  }
  /** Parse what a person types: 5/11/2026, 05-11-26, 05112026 ... -> ISO or "" */
  function dmyToIso(text) {
    text = String(text || "").trim();
    if (!text) { return ""; }
    var m = /^(\d{1,2})[\/.\-\s](\d{1,2})[\/.\-\s](\d{2}|\d{4})$/.exec(text)
         || /^(\d{2})(\d{2})(\d{4})$/.exec(text);
    if (!m) { return null; }
    var d = +m[1], mo = +m[2], y = +m[3];
    if (m[3].length === 2) { y += 2000; }
    var probe = new Date(Date.UTC(y, mo - 1, d));
    if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== mo - 1 || probe.getUTCDate() !== d) {
      return null;
    }
    return y + "-" + pad(mo) + "-" + pad(d);
  }

  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  function enhance(native) {
    if (native.dataset.dpDate || native.hasAttribute("data-native-date") || native.type !== "date") {
      return;
    }
    native.dataset.dpDate = "1";
    var wrap = document.createElement("span");
    wrap.className = "dp-date";
    var text = document.createElement("input");
    text.type = "text";
    text.inputMode = "numeric";
    text.autocomplete = "off";
    text.placeholder = "DD/MM/YYYY";
    text.className = (native.className || "") + " dp-date__text";
    text.setAttribute("aria-label", native.getAttribute("aria-label") ||
      (native.labels && native.labels[0] ? native.labels[0].textContent.trim() : t("Date")) + " (DD/MM/YYYY)");
    if (native.required) { text.required = true; }
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dp-date__btn";
    btn.setAttribute("aria-label", t("Open calendar"));
    btn.title = t("Open calendar");
    btn.appendChild(window.DPIcons ? window.DPIcons.icon("calendar-days") : document.createTextNode("📅"));

    // Keep the original sizing: a full-width date field stays full width.
    var parent = native.parentElement;
    if (parent) {
      var cs = getComputedStyle(parent);
      var inner = parent.clientWidth - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0);
      var w = native.getBoundingClientRect().width;
      if (inner > 0 && w >= inner * 0.9) { wrap.className += " dp-date--block"; }
    }
    native.parentNode.insertBefore(wrap, native);
    wrap.appendChild(text);
    wrap.appendChild(btn);
    wrap.appendChild(native);
    native.classList.add("dp-date__native");
    native.tabIndex = -1;
    native.setAttribute("aria-hidden", "true");

    function syncFromNative() {
      text.value = isoToDmy(proto.get.call(native));
      text.classList.remove("is-invalid");
      text.setCustomValidity("");
      text.disabled = native.disabled;
      text.readOnly = native.readOnly;
    }
    // Scripts that set input.value = "2026-11-12" keep working.
    Object.defineProperty(native, "value", {
      configurable: true,
      get: function () { return proto.get.call(this); },
      set: function (v) { proto.set.call(this, v); syncFromNative(); }
    });

    function commit(final) {
      var iso = dmyToIso(text.value);
      if (iso === null) {
        if (final) {
          text.classList.add("is-invalid");
          text.setCustomValidity(t("Enter the date as DD/MM/YYYY."));
        }
        return;
      }
      var min = native.min, max = native.max;
      if (iso && ((min && iso < min) || (max && iso > max))) {
        if (final) {
          text.classList.add("is-invalid");
          text.setCustomValidity(max && iso > max ? t("Future dates are not allowed.")
                                                  : t("This date is not allowed."));
          if (text.reportValidity) { text.reportValidity(); }
        }
        return;
      }
      text.classList.remove("is-invalid");
      text.setCustomValidity("");
      if (iso !== proto.get.call(native)) {
        proto.set.call(native, iso);
        fire(native, "input");
        fire(native, "change");
      }
      if (final && iso) { text.value = isoToDmy(iso); }
    }

    text.addEventListener("input", function () {
      // auto-insert slashes while typing digits: 05 -> 05/, 05/11 -> 05/11/
      var v = text.value;
      if (/^\d{2}$/.test(v) || /^\d{2}\/\d{2}$/.test(v)) { text.value = v + "/"; }
      if (/^\d{2}\/\d{2}\/\d{4}$/.test(text.value)) { commit(false); }
    });
    text.addEventListener("change", function () { commit(true); });
    text.addEventListener("blur", function () { commit(true); });
    text.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { commit(true); }
    });
    native.addEventListener("change", syncFromNative);
    native.addEventListener("focus", function () { text.focus(); });
    btn.addEventListener("click", function () {
      if (native.disabled || native.readOnly) { return; }
      try {
        if (typeof native.showPicker === "function") { native.showPicker(); return; }
      } catch (e) { /* fall through */ }
      native.style.pointerEvents = "auto";
      native.focus();
      native.click();
      setTimeout(function () { native.style.pointerEvents = ""; }, 0);
    });
    new MutationObserver(syncFromNative).observe(native, {
      attributes: true, attributeFilter: ["disabled", "readonly", "required"]
    });
    syncFromNative();
  }

  function scan(root) {
    (root || document).querySelectorAll('input[type="date"]').forEach(enhance);
  }
  window.DPDate = { scan: scan, isoToDmy: isoToDmy, dmyToIso: dmyToIso };

  function start() {
    scan(document);
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) { return; }
          if (n.matches && n.matches('input[type="date"]')) { enhance(n); }
          else if (n.querySelectorAll) { scan(n); }
        });
      });
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
