/*
 * DairyPro UI kit: translations for scripts, toasts, dialogs.
 *
 *   t("सेव्ह झाले")                    -> text in the selected language
 *   t("{n} entries saved", {n: 5})      -> placeholders
 *   DP.toast(message, "success"|"error"|"warning"|"info"|"offline"|"sync")
 *   await DP.alert(message, {title, type})
 *   if (await DP.confirm(message, {danger: true, okText: t("Delete")})) {...}
 *   const value = await DP.prompt(message, {label, required: true})
 *
 * Declarative confirmations (no page script needed):
 *   <form data-confirm="Delete this customer?" data-confirm-danger> ...
 *   <a href="..." data-confirm="...">, <button data-confirm="...">
 * The action runs ONLY after the user presses the confirm button; Escape,
 * the Cancel button or a click outside cancel it.
 *
 * Native alert()/confirm()/prompt() are not used anywhere in DairyPro.
 */
(function (global) {
  "use strict";

  // ---------------------------------------------------------------- i18n
  function norm(s) { return String(s).replace(/\s+/g, " ").trim(); }
  function t(text, params) {
    if (text === null || text === undefined) { return ""; }
    var dict = global.DP_I18N || {};
    var out = dict[norm(text)];
    if (out === undefined) { out = String(text); }
    if (params) {
      out = out.replace(/\{(\w+)\}/g, function (m, k) {
        return Object.prototype.hasOwnProperty.call(params, k) ? params[k] : m;
      });
    }
    return out;
  }
  global.t = global.t || t;

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") { e.className = attrs[k]; }
      else if (k === "text") { e.textContent = attrs[k]; }
      else if (k.indexOf("on") === 0 && typeof attrs[k] === "function") {
        e.addEventListener(k.slice(2), attrs[k]);
      } else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) {
        e.setAttribute(k, attrs[k]);
      }
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) { return; }
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }
  function icon(name) {
    if (global.DPIcons) { return global.DPIcons.icon(name, "dp-ico"); }
    return el("i", { "data-lucide": name, "class": "dp-ico" });
  }
  var TYPE_ICON = { success: "circle-check", error: "circle-x", warning: "triangle-alert",
                    info: "info", offline: "wifi-off", sync: "refresh-cw", danger: "trash-2",
                    payment: "credit-card", subscription: "crown", question: "circle-help" };

  // ---------------------------------------------------------------- toasts
  var toastBox = null;
  function toastContainer() {
    if (!toastBox || !document.body.contains(toastBox)) {
      toastBox = el("div", { "class": "dp-toasts", "aria-live": "polite", "aria-atomic": "false" });
      document.body.appendChild(toastBox);
    }
    return toastBox;
  }
  var CATEGORY = { danger: "error", error: "error", success: "success", warning: "warning",
                   info: "info", message: "info", offline: "offline", sync: "sync" };
  function toast(message, type, options) {
    options = options || {};
    type = CATEGORY[type] || type || "info";
    if (!message) { return null; }
    var item = el("div", { "class": "dp-toast dp-toast--" + type,
                           role: type === "error" ? "alert" : "status" }, [
      icon(TYPE_ICON[type] || "info"),
      el("div", { "class": "dp-toast__body" }, [
        options.title ? el("strong", { text: options.title }) : null,
        el("span", { text: String(message) })
      ]),
      el("button", { type: "button", "class": "dp-toast__close", "aria-label": t("Close"),
                     onclick: function () { close(); } }, [icon("x")])
    ]);
    if (options.action) {
      item.querySelector(".dp-toast__body").appendChild(el("button", {
        type: "button", "class": "dp-toast__action", text: options.action.label,
        onclick: function () { close(); options.action.onClick(); } }));
    }
    toastContainer().appendChild(item);
    requestAnimationFrame(function () { item.classList.add("is-in"); });
    var timeout = options.timeout !== undefined ? options.timeout
      : (type === "error" || type === "warning" ? 8000 : 4500);
    var timer = timeout ? setTimeout(close, timeout) : null;
    function close() {
      clearTimeout(timer);
      item.classList.remove("is-in");
      setTimeout(function () { if (item.parentNode) { item.parentNode.removeChild(item); } }, 220);
    }
    return { close: close, el: item };
  }

  // ---------------------------------------------------------------- dialogs
  var openDialogs = [];
  function dialog(options) {
    return new Promise(function (resolve) {
      var type = options.type || "info";
      var previous = document.activeElement;
      var input = null;
      if (options.prompt) {
        input = el(options.multiline ? "textarea" : "input", {
          "class": "dp-dialog__input", id: "dpDialogInput",
          type: options.multiline ? null : (options.inputType || "text"),
          value: options.value || "", placeholder: options.placeholder || "",
          maxlength: options.maxlength || 255, rows: options.multiline ? 3 : null
        });
        if (options.multiline && options.value) { input.value = options.value; }
      }
      var error = el("div", { "class": "dp-dialog__error", role: "alert" });
      var okBtn = el("button", { type: "button",
        "class": "dp-btn " + (options.danger ? "dp-btn--danger" : "dp-btn--primary"),
        text: options.okText || (options.cancel === false ? t("OK") : t("Confirm")) });
      var cancelBtn = options.cancel === false ? null : el("button", {
        type: "button", "class": "dp-btn dp-btn--ghost", text: options.cancelText || t("Cancel") });
      var titleId = "dpDialogTitle" + Date.now();
      var box = el("div", { "class": "dp-dialog dp-dialog--" + (options.danger ? "danger" : type),
                            role: options.cancel === false ? "alertdialog" : "dialog",
                            "aria-modal": "true", "aria-labelledby": titleId }, [
        el("div", { "class": "dp-dialog__icon" }, [icon(options.icon || TYPE_ICON[options.danger ? "danger" : type] || "info")]),
        el("h2", { "class": "dp-dialog__title", id: titleId,
                   text: options.title || (options.danger ? t("Please confirm") :
                     type === "error" ? t("Error") : type === "warning" ? t("Warning") :
                     type === "success" ? t("Success") : t("Information")) }),
        el("div", { "class": "dp-dialog__message", text: options.message || "" }),
        input ? el("label", { "class": "dp-dialog__label", "for": "dpDialogInput",
                              text: options.label || "" }) : null,
        input, error,
        el("div", { "class": "dp-dialog__actions" }, [cancelBtn, okBtn])
      ]);
      if (options.html) { box.querySelector(".dp-dialog__message").appendChild(options.html); }
      var backdrop = el("div", { "class": "dp-dialog-backdrop" }, [box]);
      document.body.appendChild(backdrop);
      openDialogs.push(backdrop);
      document.body.classList.add("dp-modal-open");
      requestAnimationFrame(function () { backdrop.classList.add("is-in"); });
      (input || (options.danger && cancelBtn) || okBtn).focus();

      function finish(value) {
        document.removeEventListener("keydown", onKey, true);
        backdrop.classList.remove("is-in");
        openDialogs.splice(openDialogs.indexOf(backdrop), 1);
        if (!openDialogs.length) { document.body.classList.remove("dp-modal-open"); }
        setTimeout(function () { if (backdrop.parentNode) { backdrop.parentNode.removeChild(backdrop); } }, 180);
        if (previous && previous.focus) { try { previous.focus(); } catch (e) {} }
        resolve(value);
      }
      function accept() {
        if (input) {
          var v = input.value.trim();
          if (options.required && !v) {
            error.textContent = options.requiredMessage || t("This field is required.");
            input.focus();
            return;
          }
          finish(v);
          return;
        }
        finish(true);
      }
      function onKey(e) {
        if (openDialogs[openDialogs.length - 1] !== backdrop) { return; }
        if (e.key === "Escape") { e.preventDefault(); finish(options.prompt ? null : false); }
        else if (e.key === "Enter" && input && !options.multiline) { e.preventDefault(); accept(); }
        else if (e.key === "Tab") {
          var focusables = box.querySelectorAll("button, input, textarea, select, a[href]");
          var first = focusables[0], last = focusables[focusables.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      }
      document.addEventListener("keydown", onKey, true);
      if (typeof options.onOpen === "function") { options.onOpen(finish); }
      okBtn.addEventListener("click", accept);
      if (cancelBtn) {
        cancelBtn.addEventListener("click", function () { finish(options.prompt ? null : false); });
      }
      backdrop.addEventListener("mousedown", function (e) {
        if (e.target === backdrop && options.cancel !== false) { finish(options.prompt ? null : false); }
      });
    });
  }
  function dpAlert(message, options) {
    options = options || {};
    return dialog(Object.assign({ message: message, cancel: false,
                                  okText: options.okText || t("OK") }, options)).then(function () { return true; });
  }
  function dpConfirm(message, options) {
    return dialog(Object.assign({ message: message, type: "question" }, options || {}));
  }
  function dpPrompt(message, options) {
    return dialog(Object.assign({ message: message, prompt: true, type: "question" }, options || {}));
  }

  // ------------------------------------------------- declarative confirm
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form && form.getAttribute && form.getAttribute("data-confirm");
    if (!msg || form.dataset.dpConfirmed === "1") { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    var submitter = e.submitter || null;
    dpConfirm(t(msg), { danger: form.hasAttribute("data-confirm-danger"),
                        okText: form.getAttribute("data-confirm-ok") ? t(form.getAttribute("data-confirm-ok")) : null,
                        title: form.getAttribute("data-confirm-title") ? t(form.getAttribute("data-confirm-title")) : null })
      .then(function (ok) {
        if (!ok) { return; }
        form.dataset.dpConfirmed = "1";
        if (submitter && submitter.name) {
          var hidden = el("input", { type: "hidden", name: submitter.name, value: submitter.value });
          form.appendChild(hidden);
        }
        if (typeof form.requestSubmit === "function") { form.requestSubmit(); }
        else { form.submit(); }
        setTimeout(function () { delete form.dataset.dpConfirmed; }, 0);
      });
  }, true);
  document.addEventListener("click", function (e) {
    var target = e.target.closest && e.target.closest("a[data-confirm], button[data-confirm]:not([type=submit])");
    if (!target || target.dataset.dpConfirmed === "1") { return; }
    if (target.tagName === "BUTTON" && target.form && target.type !== "button") { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    dpConfirm(t(target.getAttribute("data-confirm")),
              { danger: target.hasAttribute("data-confirm-danger") }).then(function (ok) {
      if (!ok) { return; }
      if (target.tagName === "A" && target.href) { global.location.href = target.href; return; }
      target.dataset.dpConfirmed = "1";
      target.click();
      delete target.dataset.dpConfirmed;
    });
  }, true);

  // ---------------------------------------------------------------- fetch
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }
  /** JSON request with CSRF, timeout and a uniform result:
   *  {ok, status, data, network, authExpired} - never throws. */
  function api(url, options) {
    options = options || {};
    var controller = global.AbortController ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, options.timeout || 20000) : null;
    var headers = Object.assign({ "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
                                options.headers || {});
    var init = { method: options.method || "GET", headers: headers, credentials: "same-origin",
                 redirect: "manual", signal: controller ? controller.signal : undefined };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (init.method !== "GET") { headers["X-CSRFToken"] = csrfToken(); }
    return fetch(url, init).then(function (res) {
      clearTimeout(timer);
      if (res.type === "opaqueredirect") {
        return { ok: false, status: 401, data: { code: "auth_required",
                 message: t("Your session has expired. Please log in again.") },
                 network: false, authExpired: true };
      }
      return res.text().then(function (text) {
        var data = {};
        try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { code: "bad_response" }; }
        if (!res.ok && !data.message) {
          data.message = res.status === 401 ? t("Your session has expired. Please log in again.")
            : t("Something went wrong. Please try again.");
        }
        return { ok: res.ok && data.code !== "bad_response", status: res.status, data: data,
                 network: false, authExpired: res.status === 401 };
      });
    }).catch(function () {
      clearTimeout(timer);
      return { ok: false, status: 0, network: true,
               data: { message: navigator.onLine ? t("Network problem. Please try again.")
                                                  : t("This action requires internet.") } };
    });
  }

  // ------------------------------------------------------------ loader
  function loader(on, text) {
    var box = document.getElementById("global-loader");
    if (!box) { return; }
    var label = box.querySelector("[data-loader-text]");
    if (label) { label.textContent = text || t("Processing..."); }
    box.style.display = on ? "flex" : "none";
  }

  global.DP = Object.assign(global.DP || {}, {
    t: t, toast: toast, alert: dpAlert, confirm: dpConfirm, prompt: dpPrompt, dialog: dialog,
    api: api, el: el, icon: icon, csrfToken: csrfToken, loader: loader
  });

  // ------------------------------------------- flash messages -> toasts
  function showFlashes() {
    var node = document.getElementById("dp-flashes");
    if (!node) { return; }
    var items = [];
    try { items = JSON.parse(node.textContent || "[]"); } catch (e) { items = []; }
    items.forEach(function (pair) { toast(pair[1], pair[0]); });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showFlashes);
  } else {
    showFlashes();
  }
})(window);
