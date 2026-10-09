/* FAT/SNF rate chart editor */
(function () {
  "use strict";

  var cfg = FSN.readJSON("chartData");
  var chart = cfg.chart;            // editing an existing chart
  var source = cfg.source;          // prefill (edit / revise / copy)
  var reviseId = cfg.revise_id;

  var SETTINGS = ["fat_min", "fat_max", "fat_step", "snf_min", "snf_max", "snf_step",
                  "std_fat", "std_snf", "std_rate", "fat_rate_step", "snf_rate_step"];

  var state = { fats: [], snfs: [], cells: {}, original: {}, dirty: false };

  function key2(v) { return Number(v).toFixed(2); }
  function cellKey(f, s) { return key2(f) + "|" + key2(s); }
  function $(id) { return document.getElementById(id); }

  function setDirty(v) { state.dirty = v; }
  window.addEventListener("beforeunload", function (e) {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  function clearErrors() {
    document.querySelectorAll(".fsn-field").forEach(function (f) {
      f.classList.remove("invalid");
      var e = f.querySelector(".fsn-err"); if (e) { e.textContent = ""; }
    });
  }
  function fieldError(field, msg) {
    var f = document.querySelector('.fsn-field[data-field="' + field + '"]');
    if (!f) { return false; }
    f.classList.add("invalid");
    f.querySelector(".fsn-err").textContent = msg;
    var input = f.querySelector("input,select"); if (input) { input.focus(); }
    return true;
  }

  function explainRule() {
    var fr = Number($("fat_rate_step").value), sr = Number($("snf_rate_step").value);
    function word(v) { return v >= 0 ? "वाढतो" : "कमी होतो"; }
    $("ruleExplain").textContent =
      "नियम: FAT प्रत्येक " + ($("fat_step").value || "?") + " ने वाढल्यास दर ₹" + Math.abs(fr || 0).toFixed(2) + " " + word(fr) +
      "; SNF प्रत्येक " + ($("snf_step").value || "?") + " ने वाढल्यास दर ₹" + Math.abs(sr || 0).toFixed(2) + " " + word(sr) +
      ". (उणे (−) मूल्य दिल्यास दर कमी होतो.)";
  }
  SETTINGS.forEach(function (k) { $(k).addEventListener("input", explainRule); });

  // ---------------------------------------------------------------
  // prefill
  // ---------------------------------------------------------------
  $("effectiveFrom").value = cfg.today;
  if (source) {
    $("chartName").value = source.chart_name || "";
    $("milkType").value = source.milk_type || "cow";
    if (chart) { $("effectiveFrom").value = source.effective_from; }
    SETTINGS.forEach(function (k) {
      if (source[k] !== null && source[k] !== undefined) { $(k).value = source[k]; }
    });
    var fs = {}, ss = {};
    (source.values || []).forEach(function (v) {
      fs[key2(v.fat)] = 1; ss[key2(v.snf)] = 1;
      state.cells[cellKey(v.fat, v.snf)] = Number(v.rate).toFixed(2);
    });
    state.fats = Object.keys(fs).sort(function (a, b) { return a - b; });
    state.snfs = Object.keys(ss).sort(function (a, b) { return a - b; });
    if (chart) { state.original = Object.assign({}, state.cells); }
    if (reviseId) { $("milkType").disabled = false; }
    if (state.fats.length) { render(); }
  }
  explainRule();

  // ---------------------------------------------------------------
  // render
  // ---------------------------------------------------------------
  function render() {
    var table = $("matrix");
    table.innerHTML = "";
    var stdKey = cellKey($("std_fat").value || 0, $("std_snf").value || 0);

    var thead = document.createElement("thead");
    var hr = document.createElement("tr");
    hr.appendChild(FSN.el("th", { "class": "corner", scope: "col" }, "FAT \\ SNF"));
    state.snfs.forEach(function (s) {
      var th = FSN.el("th", { scope: "col" }, s);
      var rm = FSN.el("button", { type: "button", "class": "axis-remove", title: "हा SNF स्तंभ काढा",
                                  "aria-label": "SNF " + s + " काढा" }, "×");
      rm.addEventListener("click", function () { removeAxis("snf", s); });
      th.appendChild(rm);
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    state.fats.forEach(function (f, r) {
      var tr = document.createElement("tr");
      var th = FSN.el("th", { scope: "row" }, f);
      var rm = FSN.el("button", { type: "button", "class": "axis-remove", title: "ही FAT ओळ काढा",
                                  "aria-label": "FAT " + f + " काढा" }, "×");
      rm.addEventListener("click", function () { removeAxis("fat", f); });
      th.appendChild(rm);
      tr.appendChild(th);
      state.snfs.forEach(function (s, c) {
        var k = cellKey(f, s);
        var td = document.createElement("td");
        if (k === stdKey) { td.classList.add("std"); }
        var input = FSN.el("input", { inputmode: "decimal", "data-r": r, "data-c": c,
                                      "aria-label": "FAT " + f + " SNF " + s + " दर" });
        input.value = state.cells[k] !== undefined ? state.cells[k] : "";
        markCell(td, k, input.value);
        input.addEventListener("input", function () {
          state.cells[k] = input.value.trim();
          setDirty(true);
          markCell(td, k, input.value.trim());
        });
        input.addEventListener("keydown", navigate);
        td.appendChild(input);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    $("matrixSection").hidden = false;
    $("saveBtn").disabled = false;
    $("matrixInfo").textContent = "(" + state.fats.length + " FAT × " + state.snfs.length +
      " SNF = " + (state.fats.length * state.snfs.length) + " दर)";
  }

  function markCell(td, k, value) {
    var bad = value === "" || isNaN(Number(value)) || Number(value) < 0;
    td.classList.toggle("bad", bad);
    td.classList.toggle("changed", !bad && state.original[k] !== undefined &&
                        Number(state.original[k]) !== Number(value));
  }

  function navigate(e) {
    var r = Number(this.dataset.r), c = Number(this.dataset.c);
    var moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], Enter: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    var m = moves[e.key];
    if (!m) { return; }
    if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && this.selectionStart !== this.selectionEnd) { return; }
    var next = document.querySelector('#matrix input[data-r="' + (r + m[0]) + '"][data-c="' + (c + m[1]) + '"]');
    if (next) { e.preventDefault(); next.focus(); next.select(); }
  }

  function removeAxis(kind, value) {
    var list = kind === "fat" ? state.fats : state.snfs;
    if (list.length <= 1) { return; }
    if (!confirm((kind === "fat" ? "FAT " : "SNF ") + value + " काढायचे?")) { return; }
    list.splice(list.indexOf(value), 1);
    setDirty(true);
    render();
  }

  function addAxis(kind) {
    var input = $(kind === "fat" ? "addFat" : "addSnf");
    var v = Number(input.value);
    if (!input.value || isNaN(v) || v <= 0 || v > 20 || Number(v.toFixed(2)) !== v) {
      FSN.notice("editorAlert", "कृपया योग्य " + kind.toUpperCase() + " मूल्य भरा (उदा. 5.1).", "error");
      input.focus();
      return;
    }
    var list = kind === "fat" ? state.fats : state.snfs;
    var k = key2(v);
    if (list.indexOf(k) !== -1) {
      FSN.notice("editorAlert", k + " आधीच आहे.", "error");
      return;
    }
    list.push(k);
    list.sort(function (a, b) { return a - b; });
    input.value = "";
    FSN.notice("editorAlert", "नवीन " + kind.toUpperCase() + " " + k + " जोडले. त्याचे दर भरा.", "ok");
    setDirty(true);
    render();
  }
  $("addFatBtn").addEventListener("click", function () { addAxis("fat"); });
  $("addSnfBtn").addEventListener("click", function () { addAxis("snf"); });

  // ---------------------------------------------------------------
  // generate (server computes, so the formula has a single source)
  // ---------------------------------------------------------------
  function settingsPayload() {
    var out = {};
    SETTINGS.forEach(function (k) { out[k] = $(k).value.trim(); });
    return out;
  }

  $("generateBtn").addEventListener("click", function () {
    clearErrors();
    var hasCells = Object.keys(state.cells).length > 0;
    if (hasCells && !confirm("नवीन तक्ता तयार केल्यास सध्याचे (हाताने बदललेले) दर बदलतील. पुढे जायचे?")) { return; }
    var btn = this;
    btn.disabled = true;
    FSN.api("/api/fat_snf/charts/preview", { method: "POST", body: settingsPayload() }).then(function (r) {
      btn.disabled = false;
      if (!r.ok) {
        if (!fieldError(r.data.field, r.data.message)) { FSN.notice("editorAlert", r.data.message, "error"); }
        return;
      }
      FSN.notice("editorAlert", "");
      state.fats = r.data.fat_values.map(key2);
      state.snfs = r.data.snf_values.map(key2);
      state.cells = {};
      r.data.matrix.forEach(function (m) { state.cells[cellKey(m.fat, m.snf)] = Number(m.rate).toFixed(2); });
      setDirty(true);
      render();
      $("matrixSection").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // ---------------------------------------------------------------
  // save
  // ---------------------------------------------------------------
  $("saveBtn").addEventListener("click", function () {
    clearErrors();
    FSN.notice("saveAlert", "");
    var name = $("chartName").value.trim();
    if (!name) { fieldError("chart_name", "चार्टचे नाव आवश्यक आहे."); return; }
    if (!$("effectiveFrom").value) { fieldError("effective_from", "लागू तारीख निवडा."); return; }

    var matrix = [], missing = 0;
    state.fats.forEach(function (f) {
      state.snfs.forEach(function (s) {
        var v = state.cells[cellKey(f, s)];
        if (v === undefined || v === "" || isNaN(Number(v)) || Number(v) < 0) { missing++; }
        matrix.push({ fat: f, snf: s, rate: v === undefined ? "" : v });
      });
    });
    if (!matrix.length) { FSN.notice("saveAlert", "आधी तक्ता तयार करा.", "error"); return; }
    if (missing) {
      FSN.notice("saveAlert", missing + " दर रिकामे/चुकीचे आहेत (लाल रंगात). सर्व दर भरा.", "error");
      var bad = document.querySelector("#matrix td.bad input"); if (bad) { bad.focus(); }
      return;
    }

    var payload = {
      chart_name: name, milk_type: $("milkType").value, effective_from: $("effectiveFrom").value,
      settings: settingsPayload(), fat_values: state.fats, snf_values: state.snfs, matrix: matrix
    };
    if (reviseId) { payload.revised_from = reviseId; }

    var btn = this;
    btn.disabled = true;
    btn.textContent = "सेव्ह होत आहे...";
    var url = chart ? "/api/fat_snf/charts/" + chart.id : "/api/fat_snf/charts";
    FSN.api(url, { method: chart ? "PUT" : "POST", body: payload, timeout: 30000 }).then(function (r) {
      btn.disabled = false;
      btn.textContent = "💾 चार्ट सेव्ह करा";
      if (r.ok) {
        setDirty(false);
        FSN.notice("saveAlert", r.data.message, "ok");
        setTimeout(function () { window.location.href = "/fat_snf/rate-chart"; }, 1200);
        return;
      }
      if (r.network) {
        FSN.notice("saveAlert", "नेटवर्क अडचण: चार्ट सेव्ह झाला की नाही हे खात्रीने सांगता येत नाही. " +
                   "चार्ट यादी तपासा, मग पुन्हा प्रयत्न करा.", "error");
        return;
      }
      if (!fieldError(r.data.field, r.data.message)) { FSN.notice("saveAlert", r.data.message, "error"); }
    });
  });
})();
