/*
 * DairyPro receipt printing.
 *
 * On screen every receipt keeps its normal layout. For printing, the
 * receipts are copied into real A4 pages:
 *   data-print-layout="simple4"  -> 4 receipts per A4 page (2 x 2)
 *   data-print-layout="fsn2"     -> 2 receipts per A4 page (1 x 2)
 *
 * Each receipt is shrunk only as much as needed to fit its cell: the font
 * scale is searched between 100% and a readable minimum (about 6.5 pt).
 * A receipt that cannot fit its cell even at the minimum gets a whole page;
 * if it still does not fit, it is printed across pages at normal size.
 * Nothing is ever clipped or dropped.
 *
 * Markup:
 *   <div class="dp-print-source" data-print-layout="simple4">
 *     <div class="dp-receipt" data-customer="12"> ...receipt... </div>
 *   </div>
 * Toolbar (optional): #dpPrintFilter (text), #dpPrintApply, #dpPrintClear,
 * #dpPrintBtn, #dpPrintInfo.
 */
(function () {
  "use strict";
  var t = window.t || function (s, p) { return s; };
  var MM = 96 / 25.4;                    // CSS px per mm
  var PAGE_W = 194, PAGE_H = 280, GAP = 4; // A4 minus 8 mm margins (1 mm safety)
  var MIN_FIT = 0.62, MAX_FIT = 1;
  var LAYOUTS = {
    simple4: { cols: 2, rows: 2 },
    fsn2: { cols: 1, rows: 2 }
  };

  /** "1-10, 15 17,36-56" -> {ok, set, error}. Empty text -> all. */
  function parseSelection(text) {
    text = String(text || "").trim();
    if (!text) { return { ok: true, set: null }; }
    var set = {};
    var parts = text.split(/[,;\s]+/).filter(Boolean);
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      var m = /^(\d+)\s*[-–]\s*(\d+)$/.exec(p);
      if (m) {
        var a = +m[1], b = +m[2];
        if (a < 1 || b < 1) { return { ok: false, error: t("Customer numbers must be 1 or more.") }; }
        if (a > b) { return { ok: false, error: t("Invalid range {range}: the first number must be smaller.", { range: p }) }; }
        if (b - a > 5000) { return { ok: false, error: t("Range {range} is too large.", { range: p }) }; }
        for (var n = a; n <= b; n++) { set[n] = true; }
      } else if (/^\d+$/.test(p)) {
        if (+p < 1) { return { ok: false, error: t("Customer numbers must be 1 or more.") }; }
        set[+p] = true;
      } else {
        return { ok: false, error: t("'{part}' is not a customer number or range (example: 1-10, 15, 17).", { part: p }) };
      }
    }
    return { ok: true, set: set };
  }

  function sources() { return Array.prototype.slice.call(document.querySelectorAll(".dp-print-source")); }

  function selectedReceipts(source, sel) {
    return Array.prototype.slice.call(source.querySelectorAll(".dp-receipt")).filter(function (r) {
      if (r.hidden) { return false; }
      if (!sel) { return true; }
      return !!sel[Number(r.getAttribute("data-customer"))];
    });
  }

  function fits(box) {
    return box.scrollHeight <= box.clientHeight + 1 && box.scrollWidth <= box.clientWidth + 1;
  }
  /** Largest scale in [MIN_FIT, MAX_FIT] that fits, or null. */
  function fit(inner, cell) {
    inner.style.setProperty("--fit", MAX_FIT);
    if (fits(cell)) { return MAX_FIT; }
    inner.style.setProperty("--fit", MIN_FIT);
    if (!fits(cell)) { return null; }
    var lo = MIN_FIT, hi = MAX_FIT;
    for (var i = 0; i < 8; i++) {
      var mid = (lo + hi) / 2;
      inner.style.setProperty("--fit", mid);
      if (fits(cell)) { lo = mid; } else { hi = mid; }
    }
    inner.style.setProperty("--fit", lo);
    return lo;
  }

  function newPage(root, layout) {
    var page = document.createElement("section");
    page.className = "dp-print-page dp-print-page--" + layout;
    root.appendChild(page);
    return page;
  }
  function newCell(page, w, h, extra) {
    var cell = document.createElement("div");
    cell.className = "dp-print-cell" + (extra ? " " + extra : "");
    cell.style.width = w + "mm";
    cell.style.height = h + "mm";
    var inner = document.createElement("div");
    inner.className = "dp-print-fit";
    cell.appendChild(inner);
    page.appendChild(cell);
    return { cell: cell, inner: inner };
  }

  var lastBuild = null;
  function build() {
    var filterInput = document.getElementById("dpPrintFilter");
    var parsed = parseSelection(filterInput ? filterInput.value : "");
    var info = document.getElementById("dpPrintInfo");
    if (!parsed.ok) {
      if (info) { info.textContent = parsed.error; info.classList.add("is-error"); }
      return { ok: false, error: parsed.error };
    }
    var old = document.getElementById("dp-print-root");
    if (old) { old.parentNode.removeChild(old); }
    var root = document.createElement("div");
    root.id = "dp-print-root";
    root.setAttribute("aria-hidden", "true");
    document.body.appendChild(root);

    var total = 0, oversize = 0, pages = 0, shown = 0;
    sources().forEach(function (source) {
      var layoutName = source.getAttribute("data-print-layout") || "simple4";
      var L = LAYOUTS[layoutName] || LAYOUTS.simple4;
      var cellW = (PAGE_W - GAP * (L.cols - 1)) / L.cols;
      var cellH = (PAGE_H - GAP * (L.rows - 1)) / L.rows;
      var perPage = L.cols * L.rows;
      var all = source.querySelectorAll(".dp-receipt");
      var list = selectedReceipts(source, parsed.set);
      // on screen, show only the selected receipts as well
      Array.prototype.forEach.call(all, function (r) {
        r.classList.toggle("dp-receipt--filtered-out", list.indexOf(r) === -1);
      });
      total += all.length;
      shown += list.length;
      var page = null, used = 0;
      list.forEach(function (receipt) {
        if (!page || used >= perPage) { page = newPage(root, layoutName); pages += 1; used = 0; }
        var slot = newCell(page, cellW, cellH);
        slot.inner.appendChild(receipt.cloneNode(true));
        if (fit(slot.inner, slot.cell) !== null) { used += 1; return; }
        // Does not fit a cell: give it a whole page.
        oversize += 1;
        page.removeChild(slot.cell);
        if (used > 0) { page = newPage(root, layoutName); pages += 1; }
        var big = newCell(page, PAGE_W, PAGE_H, "dp-print-cell--page");
        big.inner.appendChild(receipt.cloneNode(true));
        if (fit(big.inner, big.cell) === null) {
          // Still too long: print it across pages at normal size.
          big.cell.classList.add("dp-print-cell--flow");
          big.cell.style.height = "auto";
          page.classList.add("dp-print-page--flow");
          big.inner.style.setProperty("--fit", 0.85);
        }
        page.classList.add("dp-print-page--single");
        used = perPage;
      });
    });
    lastBuild = { ok: true, total: total, shown: shown, pages: pages, oversize: oversize };
    if (info) {
      info.classList.remove("is-error");
      info.textContent = t("{shown} of {total} receipts selected · {pages} A4 pages", { shown: shown, total: total, pages: pages }) +
        (oversize ? " · " + t("{n} long receipts printed on their own page", { n: oversize }) : "");
    }
    return lastBuild;
  }

  function printNow() {
    var result = build();
    if (!result.ok) {
      if (window.DP) { DP.toast(result.error, "error"); }
      return;
    }
    if (!result.shown) {
      if (window.DP) { DP.toast(t("No receipt matches the selected customer numbers."), "warning"); }
      return;
    }
    window.print();
  }

  function start() {
    if (!sources().length) { return; }
    var go = function () { build(); };
    (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(go);
    window.addEventListener("beforeprint", function () { build(); });
    var apply = document.getElementById("dpPrintApply");
    var clear = document.getElementById("dpPrintClear");
    var btn = document.getElementById("dpPrintBtn");
    var input = document.getElementById("dpPrintFilter");
    if (apply) { apply.addEventListener("click", function () { var r = build(); if (!r.ok && window.DP) { DP.toast(r.error, "error"); } }); }
    if (clear) { clear.addEventListener("click", function () { if (input) { input.value = ""; } build(); }); }
    if (input) { input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); build(); } }); }
    if (btn) { btn.addEventListener("click", printNow); }
  }

  window.DPReceiptPrint = { build: build, print: printNow, parseSelection: parseSelection };
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
})();
