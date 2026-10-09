/* FAT/SNF daily milk collection */
(function () {
  "use strict";

  var cfg = FSN.readJSON("collectionData");
  var vendors = cfg.vendors || [];
  var byId = {};
  vendors.forEach(function (v) { byId[String(v.vendor_id)] = v; });

  var state = {
    date: cfg.today, slot: cfg.slot,
    vendor: null, requestId: null, signature: null, saving: false, entries: []
  };

  function $(id) { return document.getElementById(id); }
  var form = $("entryForm");

  // ------------------------------------------------------------- header
  function fmtDate(iso) { var p = iso.split("-"); return p[2] + "/" + p[1] + "/" + p[0]; }
  function effective() {
    if ($("backdated") && $("backdated").checked) {
      return { date: $("bdDate").value || state.date, slot: $("bdSlot").value, backdated: true };
    }
    return { date: state.date, slot: state.slot, backdated: false };
  }
  function paintHeader() {
    var e = effective();
    $("hdrDate").textContent = fmtDate(e.date);
    $("hdrSlot").textContent = FSN.SLOT[e.slot] + (e.backdated ? " (जुनी नोंद)" : "");
  }

  // Client-side IST clock only to notice the slot boundary early; the server
  // remains the authority and rejects a stale date/slot.
  function istNow() {
    var parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", hour12: false
    }).formatToParts(new Date());
    var o = {};
    parts.forEach(function (p) { o[p.type] = p.value; });
    var hour = Number(o.hour) % 24;
    return { date: o.year + "-" + o.month + "-" + o.day, slot: hour >= 15 ? "evening" : "morning" };
  }
  setInterval(function () {
    var now = istNow();
    if (now.date !== state.date || now.slot !== state.slot) {
      state.date = now.date; state.slot = now.slot;
      paintHeader();
      FSN.notice("entryAlert", "वेळ बदलली: आता " + FSN.SLOT[now.slot] + " (" + fmtDate(now.date) + ").", "ok");
      loadEntries();
    }
  }, 60000);

  if ($("backdated")) {
    $("bdDate").value = state.date;
    $("bdSlot").value = state.slot;
    $("backdated").addEventListener("change", function () {
      $("backdateFields").hidden = !this.checked;
      paintHeader(); loadEntries(); schedulePreview();
    });
    $("bdDate").addEventListener("change", function () { paintHeader(); loadEntries(); schedulePreview(); });
    $("bdSlot").addEventListener("change", function () { paintHeader(); loadEntries(); });
  }

  // ------------------------------------------------------------- vendors
  var dl = $("vendorList");
  vendors.forEach(function (v) {
    dl.appendChild(FSN.el("option", { value: v.vendor_id + " - " + (v.name || "") }));
  });

  function resolveVendor(text) {
    text = (text || "").trim();
    if (!text) { return null; }
    var m = text.match(/^(\d+)/);
    if (m && byId[m[1]]) { return byId[m[1]]; }
    var lower = text.toLowerCase();
    var hits = vendors.filter(function (v) { return (v.name || "").toLowerCase() === lower; });
    return hits.length === 1 ? hits[0] : null;
  }

  function applyVendor() {
    var v = resolveVendor($("vendor").value);
    state.vendor = v;
    var sel = $("milkType");
    var info = $("vendorInfo");
    if (!v) {
      info.textContent = $("vendor").value.trim() ? "ग्राहक सापडला नाही." : "";
      return;
    }
    $("vendor").value = v.vendor_id + " - " + (v.name || "");
    info.textContent = (v.address || "") + " · " + FSN.TYPE[v.milk_type || "cow"];
    var types = v.milk_type === "both" ? ["cow", "buffalo"] : [v.milk_type || "cow"];
    sel.innerHTML = "";
    types.forEach(function (t) { sel.appendChild(FSN.el("option", { value: t }, FSN.TYPE[t])); });
    sel.disabled = types.length === 1;
    schedulePreview();
  }
  $("vendor").addEventListener("change", applyVendor);
  $("vendor").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault(); applyVendor();
      if (state.vendor) { ($("milkType").disabled ? $("fat") : $("milkType")).focus(); }
    }
  });

  // ------------------------------------------------------------- errors
  function clearErrors() {
    form.querySelectorAll(".fsn-field").forEach(function (f) {
      f.classList.remove("invalid");
      var e = f.querySelector(".fsn-err");
      if (e && e.id !== "vendorInfo") { e.textContent = ""; }
    });
  }
  function fieldError(field, msg) {
    var map = { quantity: "litres" };
    var f = form.querySelector('.fsn-field[data-field="' + (map[field] || field) + '"]');
    if (!f) { return false; }
    f.classList.add("invalid");
    var e = f.querySelector(".fsn-err"); if (e) { e.textContent = msg; }
    var input = f.querySelector("input,select"); if (input) { input.focus(); }
    return true;
  }

  // ------------------------------------------------------------- preview
  var previewTimer = null;
  function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(preview, 300); }
  ["fat", "snf", "litres", "milkType"].forEach(function (id) {
    $(id).addEventListener("input", schedulePreview);
    $(id).addEventListener("change", schedulePreview);
  });

  function preview() {
    var v = state.vendor, fat = $("fat").value.trim(), snf = $("snf").value.trim();
    $("pvRate").textContent = "–"; $("pvAmount").textContent = "–"; $("pvChart").textContent = "";
    if (!v || !fat || !snf) { return; }
    var q = new URLSearchParams({ vendor_id: v.vendor_id, milk_type: $("milkType").value,
                                  fat: fat, snf: snf, date: effective().date });
    if ($("litres").value.trim()) { q.set("litres", $("litres").value.trim()); }
    FSN.api("/api/fat_snf/rate?" + q.toString()).then(function (r) {
      if (r.ok) {
        $("pvRate").textContent = FSN.money(r.data.rate);
        $("pvAmount").textContent = r.data.amount ? FSN.money(r.data.amount) : "–";
        $("pvChart").textContent = "चार्ट: " + r.data.chart_name;
      } else if (!r.network) {
        $("pvChart").textContent = "⚠ " + r.data.message;
      }
    });
  }

  // ------------------------------------------------------------- save
  function payload(confirmWarnings) {
    var e = effective();
    return {
      vendor_id: state.vendor ? state.vendor.vendor_id : "",
      milk_type: $("milkType").value, fat: $("fat").value.trim(), snf: $("snf").value.trim(),
      litres: $("litres").value.trim(), date: e.date, slot: e.slot, backdated: e.backdated,
      confirm_warnings: !!confirmWarnings
    };
  }

  // One idempotency key per logical entry: unchanged inputs keep the key so
  // a retry can never create a second record.
  function requestIdFor(p) {
    var sig = [p.vendor_id, p.milk_type, p.fat, p.snf, p.litres, p.date, p.slot].join("|");
    if (sig !== state.signature) { state.signature = sig; state.requestId = FSN.uuid(); }
    return state.requestId;
  }

  function setSaving(on) {
    state.saving = on;
    $("saveBtn").disabled = on || cfg.mode !== "fat_snf";
    $("saveBtn").textContent = on ? "सेव्ह होत आहे..." : "💾 सेव्ह करा व पुढे (Enter)";
  }

  function onSaved(entry, replayed) {
    var msg = (replayed ? "ही नोंद आधीच सेव्ह होती: " : "✔ सेव्ह झाले: ") +
      entry.vendor_id + " " + entry.vendor_name + " – " + FSN.num(entry.quantity) + " लि. × " +
      FSN.money(entry.rate) + " = " + FSN.money(entry.amount);
    FSN.notice("entryAlert", msg, "ok");
    $("warnActions").hidden = true;
    $("vendor").value = ""; $("fat").value = ""; $("snf").value = ""; $("litres").value = "";
    $("vendorInfo").textContent = "";
    state.vendor = null; state.signature = null; state.requestId = null;
    $("pvRate").textContent = "–"; $("pvAmount").textContent = "–"; $("pvChart").textContent = "";
    $("vendor").focus();
    loadEntries();
  }

  function save(confirmWarnings, attempt) {
    if (state.saving && !attempt) { return; }
    clearErrors();
    applyVendor();
    if (!state.vendor) { fieldError("vendor_id", "कृपया ग्राहक निवडा."); return; }
    var p = payload(confirmWarnings);
    if (!p.fat || isNaN(Number(p.fat)) || Number(p.fat) <= 0) { fieldError("fat", "कृपया योग्य FAT मूल्य भरा."); return; }
    if (!p.snf || isNaN(Number(p.snf)) || Number(p.snf) <= 0) { fieldError("snf", "कृपया योग्य SNF मूल्य भरा."); return; }
    if (!p.litres || isNaN(Number(p.litres)) || Number(p.litres) <= 0) { fieldError("litres", "दूधाचे लिटर शून्यापेक्षा जास्त असावे."); return; }
    p.request_id = requestIdFor(p);

    setSaving(true);
    FSN.api("/api/fat_snf/entries", { method: "POST", body: p }).then(function (r) {
      if (r.ok) { setSaving(false); onSaved(r.data.entry, r.data.replayed); return; }
      if (r.network) { return unknownOutcome(p, confirmWarnings, attempt || 0); }
      setSaving(false);
      handleError(r);
    });
  }

  // The request may or may not have reached the server. Ask before retrying;
  // the retry reuses the same request_id, so it cannot duplicate.
  function unknownOutcome(p, confirmWarnings, attempt) {
    FSN.notice("entryAlert", "नेटवर्क अडचण – नोंद सेव्ह झाली का ते तपासत आहे...", "");
    setTimeout(function () {
      FSN.api("/api/fat_snf/requests/" + encodeURIComponent(p.request_id)).then(function (r) {
        if (r.ok && r.data.saved) { setSaving(false); onSaved(r.data.entry, true); return; }
        if (r.ok && attempt < 1) { save(confirmWarnings, attempt + 1); return; }
        setSaving(false);
        FSN.notice("entryAlert",
          "नेटवर्क कनेक्शनमध्ये अडचण आहे. नोंद अजून सेव्ह झालेली नाही. इंटरनेट आल्यावर पुन्हा 'सेव्ह' दाबा " +
          "(तीच नोंद दोनदा सेव्ह होणार नाही).", "error");
      });
    }, 1500);
  }

  function handleError(r) {
    var d = r.data || {};
    if (d.code === "needs_confirmation") {
      FSN.notice("entryAlert", "⚠ " + d.message + " (दर " + FSN.money(d.rate) + ", रक्कम " + FSN.money(d.amount) + ")", "", d.warnings);
      $("warnActions").hidden = false;
      $("confirmWarnBtn").focus();
      return;
    }
    if (d.code === "slot_changed" || d.code === "date_changed") {
      state.date = d.date; state.slot = d.slot;
      paintHeader(); loadEntries();
      FSN.notice("entryAlert", d.message + " तुमची माहिती तशीच आहे; तपासून पुन्हा सेव्ह करा.", "error");
      return;
    }
    if (d.code === "duplicate") {
      FSN.notice("entryAlert", d.message, "error");
      var a = FSN.el("a", { href: cfg.edit_url + "?date=" + effective().date, "class": "btn btn-secondary btn-sm mt-2" }, "नोंद बदला");
      $("entryAlert").firstChild.appendChild(document.createElement("br"));
      $("entryAlert").firstChild.appendChild(a);
      return;
    }
    if (d.code === "no_cell" || d.code === "no_chart") {
      fieldError("fat", "");
      FSN.notice("entryAlert", d.message + (cfg.is_owner ? " (दर चार्ट दुरुस्त करा.)" : " (मालकाला कळवा.)"), "error");
      return;
    }
    if (!fieldError(d.field, d.message)) { FSN.notice("entryAlert", d.message, "error"); }
  }

  form.addEventListener("submit", function (e) { e.preventDefault(); save(false); });
  $("confirmWarnBtn").addEventListener("click", function () { $("warnActions").hidden = true; save(true); });
  $("cancelWarnBtn").addEventListener("click", function () {
    $("warnActions").hidden = true; FSN.notice("entryAlert", ""); $("litres").focus();
  });
  $("clearBtn").addEventListener("click", function () {
    state.vendor = null; state.signature = null; clearErrors();
    FSN.notice("entryAlert", ""); $("warnActions").hidden = true;
    setTimeout(function () { $("vendorInfo").textContent = ""; $("vendor").focus(); }, 0);
  });
  // Enter moves through the fields; Enter on litres saves.
  ["fat", "snf"].forEach(function (id, i, arr) {
    $(id).addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); $(i === arr.length - 1 ? "litres" : arr[i + 1]).focus(); }
    });
  });

  // ------------------------------------------------------------- list
  function loadEntries() {
    var e = effective();
    var q = new URLSearchParams({ date: e.date, slot: e.slot });
    FSN.api("/api/fat_snf/entries?" + q.toString()).then(function (r) {
      var tbody = $("entryRows");
      tbody.innerHTML = "";
      if (!r.ok) {
        var tr = document.createElement("tr");
        tr.appendChild(FSN.el("td", { colspan: 10, "class": "text-center" }, r.data.message));
        tbody.appendChild(tr);
        return;
      }
      state.entries = r.data.entries;
      var t = r.data.totals;
      $("stCount").textContent = t.count;
      $("stLitres").textContent = FSN.num(t.litres);
      $("stAmount").textContent = FSN.money(t.amount);
      $("stSaved").textContent = t.saved;
      if (!state.entries.length) {
        var empty = document.createElement("tr");
        empty.appendChild(FSN.el("td", { colspan: 10, "class": "text-center text-muted" }, "अजून नोंद नाही."));
        tbody.appendChild(empty);
        return;
      }
      state.entries.slice().reverse().forEach(function (en) {
        var tr = document.createElement("tr");
        [en.vendor_id, en.vendor_name, FSN.TYPE[en.milk_type]].forEach(function (v) { tr.appendChild(FSN.el("td", {}, v)); });
        [FSN.num(en.fat), FSN.num(en.snf), FSN.num(en.quantity), FSN.money(en.rate), FSN.money(en.amount)]
          .forEach(function (v) { tr.appendChild(FSN.el("td", { "class": "num" }, v)); });
        var st = FSN.el("td");
        st.appendChild(FSN.el("span", { "class": "fsn-badge " + en.status }, en.status === "final" ? "अंतिम" : "सेव्ह"));
        tr.appendChild(st);
        var act = FSN.el("td");
        act.appendChild(FSN.el("a", { "class": "btn btn-secondary btn-sm",
          href: cfg.edit_url + "?date=" + en.collection_date + "#entry-" + en.id }, "बदला"));
        tr.appendChild(act);
        tbody.appendChild(tr);
      });
    });
  }
  $("refreshBtn").addEventListener("click", loadEntries);

  // ------------------------------------------------------------- finalize
  var modal = $("finalModal");
  modal.querySelectorAll("[data-close]").forEach(function (b) {
    b.addEventListener("click", function () { modal.classList.remove("open"); });
  });

  $("finalBtn").addEventListener("click", function () {
    var saved = state.entries.filter(function (e) { return e.status === "saved"; });
    var litres = 0, amount = 0;
    state.entries.forEach(function (e) { litres += Number(e.quantity); amount += Number(e.amount); });
    var seen = {};
    state.entries.forEach(function (e) { seen[String(e.vendor_id)] = 1; });
    var missing = vendors.filter(function (v) { return !seen[String(v.vendor_id)]; }).length;
    var e = effective();
    var body = $("finalBody");
    body.innerHTML = "";
    body.appendChild(FSN.el("p", {}, fmtDate(e.date) + " · " + FSN.SLOT[e.slot]));
    var ul = FSN.el("ul");
    ul.appendChild(FSN.el("li", {}, "एकूण नोंदी: " + state.entries.length + " (अंतिम करायच्या: " + saved.length + ")"));
    ul.appendChild(FSN.el("li", {}, "एकूण लिटर: " + litres.toFixed(2)));
    ul.appendChild(FSN.el("li", {}, "एकूण रक्कम: " + FSN.money(amount)));
    body.appendChild(ul);
    if (missing) {
      body.appendChild(FSN.el("div", { "class": "fsn-notice" },
        missing + " ग्राहकांची या वेळेची नोंद नाही. ते बरोबर आहे का ते तपासा."));
    }
    if (!saved.length) {
      body.appendChild(FSN.el("div", { "class": "fsn-notice ok" }, "अंतिम करण्यासाठी नवीन नोंद नाही."));
    }
    FSN.notice("finalAlert", "");
    $("finalConfirmBtn").disabled = !saved.length;
    modal.classList.add("open");
    $("finalConfirmBtn").focus();
  });

  $("finalConfirmBtn").addEventListener("click", function () {
    var btn = this, e = effective();
    btn.disabled = true;
    FSN.api("/api/fat_snf/finalize", { method: "POST", body: { date: e.date, slot: e.slot } }).then(function (r) {
      if (r.ok) {
        modal.classList.remove("open");
        FSN.notice("entryAlert", r.data.message, "ok");
        loadEntries();
        return;
      }
      btn.disabled = false;
      // Finalizing is idempotent, so retrying after a network error is safe.
      FSN.notice("finalAlert", r.data.message, "error");
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { modal.classList.remove("open"); }
  });

  paintHeader();
  loadEntries();
  $("vendor").focus();
})();
