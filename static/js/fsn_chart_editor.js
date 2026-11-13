/* DairyPro FAT/SNF chart editor.
 * The matrix range and the base (standard) FAT/SNF depend on the animal:
 *   cow     base 3.5 / 8.5, FAT 2.5-5.0, SNF 8.0-9.0
 *   buffalo base 6.0 / 9.0, FAT 5.0-11.0, SNF 8.0-10.5
 *   both    base 3.5 / 8.5, FAT 2.5-11.0, SNF 8.0-10.5
 * (the same limits are enforced by the server). FAT and SNF share one
 * per-0.1 rate increment. */
(function () {
  "use strict";
  var cfg = FSN.readJSON("chartData") || {};
  var chart = cfg.chart || null;
  var source = cfg.source || null;
  var reviseId = cfg.revise_id || null;
  var RULES = cfg.rules || {};
  var $ = function (id) { return document.getElementById(id); };
  var state = { fats: [], snfs: [], cells: {}, original: {}, dirty: false };

  function rules() {
    var type = $("milkType").value || "cow";
    return RULES[type] || RULES.cow || { std_fat: "3.50", std_snf: "8.50", fat_min: "2.50", fat_max: "5.00", snf_min: "8.00", snf_max: "9.00" };
  }
  function fixed() {
    var r = rules();
    return { fat_min: r.fat_min, fat_max: r.fat_max, fat_step: "0.1", snf_min: r.snf_min, snf_max: r.snf_max,
             snf_step: "0.1", std_fat: r.std_fat, std_snf: r.std_snf };
  }
  function k(v) { return Number(v).toFixed(2); }
  function cellKey(f, s) { return k(f) + "|" + k(s); }
  function setDirty(v) { state.dirty = v; }
  function setFixedFields() {
    var F = fixed();
    Object.keys(F).forEach(function (id) { if ($(id)) { $(id).value = F[id]; } });
    syncSharedStep();
  }
  function syncSharedStep() { if ($("snf_rate_step") && $("fat_rate_step")) { $("snf_rate_step").value = $("fat_rate_step").value; } }
  function moneyRate(f, s) {
    var r = rules();
    var base = Number($("std_rate").value);
    var step = Number($("fat_rate_step").value);
    return Math.round((base + ((Number(f) - Number(r.std_fat)) / 0.1) * step +
                       ((Number(s) - Number(r.std_snf)) / 0.1) * step + Number.EPSILON) * 100) / 100;
  }
  function ruleExplain() {
    var r = rules();
    var step = Number($("fat_rate_step").value || 0);
    var stdFat = Number(r.std_fat).toFixed(1), stdSnf = Number(r.std_snf).toFixed(1);
    if ($("stdRateLabel")) {
      $("stdRateLabel").textContent = t("FAT {fat} आणि SNF {snf} साठी आधार दर (₹/लिटर)", { fat: stdFat, snf: stdSnf });
    }
    if ($("rangeInfo")) {
      $("rangeInfo").textContent = t("FAT {fmin}–{fmax}, SNF {smin}–{smax} (0.1 च्या फरकाने)", {
        fmin: Number(r.fat_min).toFixed(1), fmax: Number(r.fat_max).toFixed(1),
        smin: Number(r.snf_min).toFixed(1), smax: Number(r.snf_max).toFixed(1) });
    }
    $("ruleExplain").textContent = t("आधार: FAT {fat} / SNF {snf} = ₹{rate}. FAT किंवा SNF प्रत्येक 0.1 ने बदलल्यास दर ₹{step} ने {direction}.", {
      fat: stdFat, snf: stdSnf, rate: (Number($("std_rate").value || 0)).toFixed(2),
      step: Math.abs(step).toFixed(2), direction: step >= 0 ? t("वाढेल") : t("कमी होईल") });
  }
  function clearErrors() {
    document.querySelectorAll(".fsn-field").forEach(function (f) { f.classList.remove("invalid"); var e = f.querySelector(".fsn-err"); if (e) { e.textContent = ""; } });
  }
  function fieldError(field, msg) {
    var f = document.querySelector('.fsn-field[data-field="' + field + '"]');
    if (!f) { return false; }
    f.classList.add("invalid"); var e = f.querySelector(".fsn-err"); if (e) { e.textContent = msg; }
    var input = f.querySelector("input,select"); if (input) { input.focus(); } return true;
  }
  function settingsPayload() {
    setFixedFields(); syncSharedStep();
    var out = {};
    ["fat_min", "fat_max", "fat_step", "snf_min", "snf_max", "snf_step", "std_fat", "std_snf", "std_rate", "fat_rate_step", "snf_rate_step"]
      .forEach(function (id) { out[id] = $(id).value.trim(); });
    return out;
  }
  function render() {
    var r = rules();
    var table = $("matrix"); table.innerHTML = "";
    var thead = document.createElement("thead"), hr = document.createElement("tr");
    hr.appendChild(FSN.el("th", { "class": "corner", "scope": "col" }, "FAT \\ SNF"));
    state.snfs.forEach(function (s) {
      var th = FSN.el("th", { scope: "col" }, s);
      var rm = FSN.el("button", { type: "button", "class": "axis-remove", title: t("SNF स्तंभ काढा"), "aria-label": t("SNF {value} काढा", { value: s }) });
      rm.appendChild(DPIcons.icon("x"));
      rm.addEventListener("click", function () { removeAxis("snf", s); }); th.appendChild(rm); hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tbody = document.createElement("tbody");
    state.fats.forEach(function (f, row) {
      var tr = document.createElement("tr"), th = FSN.el("th", { scope: "row" }, f);
      var rm = FSN.el("button", { type: "button", "class": "axis-remove", title: t("FAT ओळ काढा"), "aria-label": t("FAT {value} काढा", { value: f }) });
      rm.appendChild(DPIcons.icon("x"));
      rm.addEventListener("click", function () { removeAxis("fat", f); }); th.appendChild(rm); tr.appendChild(th);
      state.snfs.forEach(function (s, c) {
        var key = cellKey(f, s), td = document.createElement("td");
        var input = FSN.el("input", { type: "number", min: "0", step: "0.01", inputmode: "decimal", "data-r": row, "data-c": c,
                                      "aria-label": t("FAT {fat} SNF {snf} दर", { fat: f, snf: s }) });
        if (key === cellKey(r.std_fat, r.std_snf)) { td.classList.add("standard-rate"); }
        input.value = state.cells[key] === undefined ? "" : state.cells[key]; markCell(td, key, input.value);
        input.addEventListener("input", function () { state.cells[key] = input.value.trim(); setDirty(true); markCell(td, key, input.value.trim()); });
        input.addEventListener("keydown", navigate); td.appendChild(input); tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); $("matrixSection").hidden = false; $("saveBtn").disabled = false;
    $("matrixInfo").textContent = "(" + state.fats.length + " FAT × " + state.snfs.length + " SNF = " + (state.fats.length * state.snfs.length) + " " + t("दर") + ")";
  }
  function markCell(td, key, value) {
    var bad = value === "" || !Number.isFinite(Number(value)) || Number(value) < 0;
    var changed = !bad && state.original[key] !== undefined && Number(state.original[key]) !== Number(value);
    td.classList.toggle("bad", bad);
    td.classList.toggle("changed", changed);
    td.classList.toggle("changed-rate", changed);
  }
  function navigate(e) {
    var r = Number(this.dataset.r), c = Number(this.dataset.c);
    var moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], Enter: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }, m = moves[e.key];
    if (!m) { return; }
    if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && this.selectionStart !== this.selectionEnd) { return; }
    var next = document.querySelector('#matrix input[data-r="' + (r + m[0]) + '"][data-c="' + (c + m[1]) + '"]');
    if (next) { e.preventDefault(); next.focus(); next.select(); }
  }
  function removeAxis(kind, value) {
    var list = kind === "fat" ? state.fats : state.snfs;
    if (list.length <= 1) { return; }
    DP.confirm(t("{axis} {value} काढायचे?", { axis: kind === "fat" ? "FAT" : "SNF", value: value }),
               { danger: true, okText: t("Delete") }).then(function (ok) {
      if (!ok) { return; }
      list.splice(list.indexOf(value), 1); setDirty(true); render();
    });
  }
  function addAxis(kind) {
    var r = rules();
    var list = kind === "fat" ? state.fats : state.snfs, input = $(kind === "fat" ? "addFat" : "addSnf");
    var lo = Number(kind === "fat" ? r.fat_min : r.snf_min), hi = Number(kind === "fat" ? r.fat_max : r.snf_max);
    var v = input.value.trim() ? Number(input.value) : ((list.length ? Math.max.apply(null, list.map(Number)) : lo) + 0.1);
    v = Math.round(v * 100) / 100;
    if (!Number.isFinite(v) || v < lo || v > hi || Math.abs(v * 10 - Math.round(v * 10)) > 0.00001) {
      FSN.notice("editorAlert", t("{axis} {min} ते {max} दरम्यान 0.1 च्या पटीतील मूल्य भरा.", { axis: kind.toUpperCase(), min: lo.toFixed(1), max: hi.toFixed(1) }), "error");
      input.focus();
      return;
    }
    var key = k(v);
    if (list.indexOf(key) !== -1) { FSN.notice("editorAlert", t("{value} आधीच आहे.", { value: key }), "error"); return; }
    list.push(key); list.sort(function (a, b) { return Number(a) - Number(b); });
    // Fill only missing cells; manually edited and existing cells are never overwritten.
    state.fats.forEach(function (f) { state.snfs.forEach(function (s) { var ck = cellKey(f, s); if (state.cells[ck] === undefined || state.cells[ck] === "") { state.cells[ck] = moneyRate(f, s).toFixed(2); } }); });
    input.value = ""; setDirty(true);
    FSN.notice("editorAlert", t("नवीन {axis} {value} जोडला. नवीन दर आपोआप भरले आहेत; गरज असल्यास बदलू शकता.", { axis: kind.toUpperCase(), value: key }), "ok");
    render();
  }
  function generate() {
    clearErrors(); FSN.notice("editorAlert", ""); setFixedFields();
    var base = Number($("std_rate").value), step = Number($("fat_rate_step").value);
    if (!Number.isFinite(base) || base < 0) { fieldError("std_rate", t("कृपया शून्य किंवा त्यापेक्षा मोठा आधार दर भरा.")); return; }
    if (!Number.isFinite(step)) { fieldError("fat_rate_step", t("कृपया योग्य दर बदल भरा.")); return; }
    var ask = Object.keys(state.cells).length
      ? DP.confirm(t("नवीन तक्ता तयार केल्यास सध्याचे आणि हाताने बदललेले दर बदलतील. पुढे जायचे?"), { type: "warning", okText: t("Continue") })
      : Promise.resolve(true);
    ask.then(function (ok) {
      if (!ok) { return; }
      if (navigator.onLine === false) { DP.toast(t("This action requires internet."), "offline"); return; }
      var btn = $("generateBtn"); btn.disabled = true;
      FSN.api("/api/fat_snf/charts/preview", { method: "POST", body: settingsPayload() }).then(function (res) {
        btn.disabled = false;
        if (!res.ok) { FSN.notice("editorAlert", res.data.message || t("तक्ता तयार करता आला नाही."), "error"); return; }
        state.fats = res.data.fat_values.map(k); state.snfs = res.data.snf_values.map(k); state.cells = {};
        res.data.matrix.forEach(function (m) { state.cells[cellKey(m.fat, m.snf)] = Number(m.rate).toFixed(2); });
        setDirty(true); render();
        var section = $("matrixSection"); if (section.scrollIntoView) { section.scrollIntoView({ behavior: "smooth", block: "start" }); }
      });
    });
  }
  function save() {
    clearErrors(); FSN.notice("saveAlert", ""); var name = $("chartName").value.trim();
    if (!name) { fieldError("chart_name", t("चार्टचे नाव आवश्यक आहे.")); return; }
    if (!$("effectiveFrom").value) { fieldError("effective_from", t("लागू तारीख निवडा.")); return; }
    if (navigator.onLine === false) { DP.toast(t("This action requires internet."), "offline"); return; }
    var matrix = [], missing = 0;
    state.fats.forEach(function (f) { state.snfs.forEach(function (s) { var v = state.cells[cellKey(f, s)]; if (v === undefined || v === "" || !Number.isFinite(Number(v)) || Number(v) < 0) { missing++; } matrix.push({ fat: f, snf: s, rate: v === undefined ? "" : v }); }); });
    if (!matrix.length) { FSN.notice("saveAlert", t("आधी तक्ता तयार करा."), "error"); return; }
    if (missing) {
      FSN.notice("saveAlert", t("{n} दर रिकामे किंवा चुकीचे आहेत. लाल रंगातील दर दुरुस्त करा.", { n: missing }), "error");
      var bad = document.querySelector("#matrix td.bad input"); if (bad) { bad.focus(); } return;
    }
    var payload = { chart_name: name, milk_type: $("milkType").value, effective_from: $("effectiveFrom").value, settings: settingsPayload(), fat_values: state.fats, snf_values: state.snfs, matrix: matrix };
    if (reviseId) { payload.revised_from = reviseId; }
    var btn = $("saveBtn"); btn.disabled = true; var oldHtml = btn.innerHTML; btn.textContent = t("सेव्ह होत आहे...");
    var url = chart ? "/api/fat_snf/charts/" + chart.id : "/api/fat_snf/charts";
    FSN.api(url, { method: chart ? "PUT" : "POST", body: payload, timeout: 30000 }).then(function (res) {
      btn.disabled = false; btn.innerHTML = oldHtml;
      if (res.ok) {
        setDirty(false);
        DP.toast(res.data.message || t("चार्ट सेव्ह झाला."), "success");
        FSN.notice("saveAlert", res.data.message || t("चार्ट सेव्ह झाला."), "ok");
        setTimeout(function () { window.location.href = "/fat_snf/rate-chart"; }, 1200);
        return;
      }
      var msg = res.data.message || t("चार्ट सेव्ह करताना अडचण आली.");
      if (res.network) { msg = t("नेटवर्क अडचण: चार्ट सेव्ह झाला की नाही हे खात्रीने सांगता येत नाही. चार्ट यादी तपासा."); }
      if (!fieldError(res.data.field, msg)) { FSN.notice("saveAlert", msg, "error"); }
    });
  }
  function onTypeChange() {
    var hadMatrix = state.fats.length && state.snfs.length;
    setFixedFields(); ruleExplain();
    if (!hadMatrix || chart) { if (hadMatrix) { render(); } return; }
    DP.confirm(t("दुधाचा प्रकार बदलल्याने FAT/SNF श्रेणी बदलते. नवीन श्रेणीनुसार तक्ता पुन्हा तयार करायचा?"),
               { type: "question", okText: t("Continue") }).then(function (ok) {
      if (ok) { state.cells = {}; generate(); } else { render(); }
    });
  }
  function prefill() {
    $("effectiveFrom").value = cfg.today || "";
    if (source) {
      $("chartName").value = source.chart_name || ""; $("milkType").value = source.milk_type || "cow";
      if (chart && source.effective_from) { $("effectiveFrom").value = source.effective_from; }
      if (source.std_rate !== undefined && source.std_rate !== null) { $("std_rate").value = source.std_rate; }
      var step = source.fat_rate_step !== undefined && source.fat_rate_step !== null ? source.fat_rate_step : (source.snf_rate_step !== undefined ? source.snf_rate_step : "0.30");
      $("fat_rate_step").value = step; syncSharedStep();
      var fs = {}, ss = {};
      (source.values || []).forEach(function (v) { fs[k(v.fat)] = 1; ss[k(v.snf)] = 1; state.cells[cellKey(v.fat, v.snf)] = Number(v.rate).toFixed(2); });
      state.fats = Object.keys(fs).sort(function (a, b) { return Number(a) - Number(b); });
      state.snfs = Object.keys(ss).sort(function (a, b) { return Number(a) - Number(b); });
      if (chart) { state.original = Object.assign({}, state.cells); }
    }
    setFixedFields();
    if (state.fats.length && state.snfs.length) { render(); }
    ruleExplain();
  }
  $("fat_rate_step").addEventListener("input", function () { syncSharedStep(); ruleExplain(); });
  $("std_rate").addEventListener("input", ruleExplain);
  $("milkType").addEventListener("change", onTypeChange);
  $("generateBtn").addEventListener("click", generate);
  $("addFatBtn").addEventListener("click", function () { addAxis("fat"); });
  $("addSnfBtn").addEventListener("click", function () { addAxis("snf"); });
  $("saveBtn").addEventListener("click", save);
  window.addEventListener("beforeunload", function (e) { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
  prefill();
})();
