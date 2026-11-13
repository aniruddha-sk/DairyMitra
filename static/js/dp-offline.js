/*
 * DairyPro offline queue + synchronisation engine.
 *
 * Loaded by every page AND by the service worker (Background Sync), so the
 * same rules apply wherever a sync runs.
 *
 * Guarantees
 *  - A record is reported "Saved on this device" only after the IndexedDB
 *    transaction has COMPLETED (not when the request was queued).
 *  - Every record carries tenant_id (the dairy), user role/staff id, record
 *    type, local id, idempotency key, timestamps, status and retry count.
 *  - Only records of the dairy that is logged in RIGHT NOW (confirmed by
 *    GET /api/session before every run) are sent; the server checks the
 *    X-DP-Tenant header again. Another dairy's records stay untouched on
 *    the device until that dairy logs in.
 *  - A record leaves the queue only after the server explicitly confirmed
 *    acceptance ({"saved": true}); HTML pages, redirects (expired session),
 *    warnings and errors never count as success.
 *  - Retries reuse the record's idempotency key, so a request that reached
 *    the server before the connection dropped is never stored twice.
 *  - One sync at a time across all tabs and the service worker
 *    (Web Locks API, with an IndexedDB lease as fallback).
 *  - Temporary failures retry with exponential backoff; invalid data is not
 *    retried automatically but kept for review; nothing is ever discarded
 *    without the user asking.
 */
(function (global) {
  "use strict";

  var IS_WINDOW = typeof document !== "undefined";
  var DB_NAME = "DairyProOffline";
  var DB_VERSION = 1;
  var LEGACY_DB = "DairyMitrOfflineDB";   // queue store of the old DairyMitra pages
  var SYNC_TAG = "dairypro-sync";
  var MAX_AUTO_RETRIES = 8;
  var KEEP_SYNCED_MS = 3 * 24 * 3600 * 1000;
  var STALE_SYNCING_MS = 2 * 60 * 1000;
  var REQUEST_TIMEOUT_MS = 20000;
  var S = { PENDING: "pending", SYNCING: "syncing", SYNCED: "synced", FAILED: "failed",
            REVIEW: "needs_review", BLOCKED: "blocked" };
  var OPEN = [S.PENDING, S.SYNCING, S.FAILED, S.REVIEW, S.BLOCKED];

  var t = function (s, p) { return global.t ? global.t(s, p) : String(s).replace(/\{(\w+)\}/g, function (m, k) { return p && k in p ? p[k] : m; }); };

  function now() { return Date.now(); }
  function uuid() {
    if (global.crypto && global.crypto.randomUUID) { return global.crypto.randomUUID(); }
    var b = new Uint8Array(16);
    (global.crypto || global.msCrypto).getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return (x + 0x100).toString(16).slice(1); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function StorageError(message, cause) {
    var e = new Error(message);
    e.code = "storage_unavailable";
    e.cause = cause;
    return e;
  }

  // ------------------------------------------------------------ IndexedDB
  var dbPromise = null;
  function openDB() {
    if (dbPromise) { return dbPromise; }
    dbPromise = new Promise(function (resolve, reject) {
      if (!global.indexedDB) {
        reject(StorageError("IndexedDB is not available in this browser."));
        return;
      }
      var req;
      try { req = global.indexedDB.open(DB_NAME, DB_VERSION); }
      catch (e) { reject(StorageError("IndexedDB could not be opened.", e)); return; }
      var settled = false;
      var timer = setTimeout(function () {
        if (!settled) { settled = true; reject(StorageError("IndexedDB did not respond.")); }
      }, 8000);
      req.onupgradeneeded = function (e) {
        var db = e.target.result;
        if (!db.objectStoreNames.contains("queue")) {
          var q = db.createObjectStore("queue", { keyPath: "local_id" });
          q.createIndex("tenant", "tenant_id", { unique: false });
          q.createIndex("tenant_status", ["tenant_id", "status"], { unique: false });
          q.createIndex("dedupe", "dedupe_key", { unique: false });
          q.createIndex("idempotency", "idempotency_key", { unique: true });
        }
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", { keyPath: "key" });
        }
      };
      req.onsuccess = function (e) {
        if (settled) { return; }
        settled = true; clearTimeout(timer);
        var db = e.target.result;
        db.onversionchange = function () { db.close(); dbPromise = null; };
        db.onclose = function () { dbPromise = null; };
        resolve(db);
      };
      req.onerror = function (e) {
        if (settled) { return; }
        settled = true; clearTimeout(timer);
        reject(StorageError("IndexedDB could not be opened.", e.target.error));
      };
      req.onblocked = function () { /* another tab holds an old version; wait for timeout */ };
    });
    dbPromise.catch(function () { dbPromise = null; });
    return dbPromise;
  }

  /** Run fn(stores) inside ONE transaction; resolves only after the
   *  transaction COMPLETED (data is on disk), rejects on error/abort. */
  function tx(names, mode, fn) {
    return openDB().then(function (db) {
      return new Promise(function (resolve, reject) {
        var transaction;
        try { transaction = db.transaction(names, mode); }
        catch (e) { reject(StorageError("IndexedDB transaction failed.", e)); return; }
        var stores = {};
        [].concat(names).forEach(function (n) { stores[n] = transaction.objectStore(n); });
        var result;
        try { result = fn(stores, transaction); }
        catch (e) { try { transaction.abort(); } catch (x) {} reject(e); return; }
        transaction.oncomplete = function () {
          resolve(result && result.__box ? result.value : result);
        };
        transaction.onerror = function (e) {
          reject(e.target.error && e.target.error.__dp ? e.target.error : StorageError("Could not save on this device.", e.target.error));
        };
        transaction.onabort = function (e) {
          var err = transaction.__dpError || StorageError("Could not save on this device.", e.target.error);
          reject(err);
        };
      });
    });
  }
  function req2promise(r) {
    return new Promise(function (resolve, reject) {
      r.onsuccess = function () { resolve(r.result); };
      r.onerror = function () { reject(r.error); };
    });
  }
  /** Mutable box: lets a transaction callback hand back a value that is
   *  produced asynchronously inside the transaction. */
  function box() { return { __box: true, value: undefined }; }

  function getMeta(key) {
    return tx("meta", "readonly", function (s) {
      var out = box();
      s.meta.get(key).onsuccess = function (e) { out.value = e.target.result ? e.target.result.value : undefined; };
      return out;
    });
  }
  function setMeta(key, value) {
    return tx("meta", "readwrite", function (s) { s.meta.put({ key: key, value: value }); });
  }

  // ------------------------------------------------------------ queue API
  /**
   * Store a record on this device.
   * rec: {type, endpoint, method?, payload, tenant_id, role?, staff_id?,
   *       idempotency_key?, idempotency_field?, dedupe_key?, label?}
   * Resolves with the stored record AFTER the write completed.
   * Rejects: code "duplicate_local" (same entry already waiting),
   *          code "storage_unavailable" (could not save on this device).
   */
  function enqueue(rec) {
    if (!rec || !rec.type || !rec.endpoint || !rec.payload || !rec.tenant_id) {
      return Promise.reject(new Error("Incomplete offline record."));
    }
    var key = rec.idempotency_key || uuid();
    var record = {
      local_id: uuid(),
      idempotency_key: key,
      idempotency_field: rec.idempotency_field || "client_request_id",
      tenant_id: String(rec.tenant_id),
      role: rec.role || null,
      staff_id: rec.staff_id || null,
      type: rec.type,
      endpoint: rec.endpoint,
      method: rec.method || "POST",
      payload: Object.assign({}, rec.payload),
      dedupe_key: rec.dedupe_key ? String(rec.tenant_id) + "|" + rec.dedupe_key : null,
      label: rec.label || rec.type,
      status: rec.status || S.PENDING,
      legacy: !!rec.legacy,
      created_at: rec.created_at || now(),
      updated_at: now(),
      retry_count: 0,
      next_attempt_at: 0,
      last_error: rec.last_error || null,
      last_status: null,
      server: null
    };
    record.payload[record.idempotency_field] = key;
    return tx("queue", "readwrite", function (s, transaction) {
      var out = box();
      function add() {
        s.queue.add(record).onsuccess = function () { out.value = record; };
      }
      if (!record.dedupe_key) { add(); return out; }
      s.queue.index("dedupe").getAll(record.dedupe_key).onsuccess = function (e) {
        var open = (e.target.result || []).filter(function (r) { return OPEN.indexOf(r.status) !== -1; });
        if (open.length) {
          var err = new Error(t("This entry is already saved on this device and waiting for synchronization."));
          err.code = "duplicate_local";
          err.__dp = true;
          err.existing = open[0];
          transaction.__dpError = err;
          transaction.abort();
          return;
        }
        add();
      };
      return out;
    }).then(function (stored) {
      notify("queued", { record: stored });
      requestSync();
      return stored;
    });
  }

  function all() {
    return tx("queue", "readonly", function (s) {
      var out = box();
      s.queue.getAll().onsuccess = function (e) { out.value = e.target.result || []; };
      return out;
    });
  }
  function forTenant(tenant) {
    return all().then(function (rows) {
      return rows.filter(function (r) { return r.tenant_id === String(tenant); })
                 .sort(function (a, b) { return a.created_at - b.created_at; });
    });
  }
  function summary(tenant) {
    return all().then(function (rows) {
      var out = { pending: 0, syncing: 0, failed: 0, review: 0, blocked: 0, synced: 0,
                  other_tenants: 0, total_open: 0 };
      rows.forEach(function (r) {
        var mine = r.tenant_id === String(tenant);
        var open = OPEN.indexOf(r.status) !== -1;
        if (!mine) { if (open) { out.other_tenants += 1; } return; }
        if (r.status === S.PENDING) { out.pending += 1; }
        else if (r.status === S.SYNCING) { out.syncing += 1; }
        else if (r.status === S.FAILED) { out.failed += 1; }
        else if (r.status === S.REVIEW) { out.review += 1; }
        else if (r.status === S.BLOCKED) { out.blocked += 1; }
        else if (r.status === S.SYNCED) { out.synced += 1; }
        if (open) { out.total_open += 1; }
      });
      return out;
    });
  }
  function update(localId, patch) {
    return tx("queue", "readwrite", function (s) {
      var out = box();
      s.queue.get(localId).onsuccess = function (e) {
        var r = e.target.result;
        if (!r) { return; }
        Object.assign(r, patch, { updated_at: now() });
        s.queue.put(r);
        out.value = r;
      };
      return out;
    });
  }
  /** Only ever called from an explicit user action (discard button). */
  function remove(localId) {
    return tx("queue", "readwrite", function (s) { s.queue.delete(localId); });
  }

  // ------------------------------------------------------------ events
  function notify(type, detail) {
    detail = Object.assign({ type: type }, detail || {});
    if (IS_WINDOW) {
      try { global.dispatchEvent(new CustomEvent("dp:sync", { detail: detail })); } catch (e) {}
    } else if (global.clients && global.clients.matchAll) {
      global.clients.matchAll({ includeUncontrolled: true, type: "window" }).then(function (list) {
        list.forEach(function (c) { c.postMessage({ dpSync: detail }); });
      });
    }
  }

  function requestSync() {
    if (IS_WINDOW && global.navigator.serviceWorker && global.navigator.serviceWorker.ready) {
      global.navigator.serviceWorker.ready.then(function (reg) {
        if (reg.sync && reg.sync.register) { return reg.sync.register(SYNC_TAG); }
      }).catch(function () {});
    }
    if (IS_WINDOW) { schedule(1500); }
  }

  // ------------------------------------------------------------ locking
  function withLock(fn) {
    var nav = global.navigator;
    if (nav && nav.locks && nav.locks.request) {
      return nav.locks.request("dairypro-sync", { ifAvailable: true }, function (lock) {
        if (!lock) { return { skipped: "locked" }; }
        return fn();
      });
    }
    // Fallback: a lease in IndexedDB shared by tabs and the service worker.
    var owner = uuid();
    return tx("meta", "readwrite", function (s) {
      var out = box();
      s.meta.get("sync_lease").onsuccess = function (e) {
        var lease = e.target.result && e.target.result.value;
        if (lease && lease.expires > now() && lease.owner !== owner) { out.value = false; return; }
        s.meta.put({ key: "sync_lease", value: { owner: owner, expires: now() + 90000 } });
        out.value = true;
      };
      return out;
    }).then(function (got) {
      if (!got) { return { skipped: "locked" }; }
      return Promise.resolve().then(fn).then(function (r) {
        return setMeta("sync_lease", null).then(function () { return r; });
      }, function (err) {
        return setMeta("sync_lease", null).then(function () { throw err; });
      });
    });
  }

  // ------------------------------------------------------------ network
  function request(url, options) {
    options = options || {};
    var controller = global.AbortController ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS) : null;
    var headers = Object.assign({ "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
                                options.headers || {});
    var init = { method: options.method || "GET", headers: headers, credentials: "same-origin",
                 redirect: "manual", cache: "no-store",
                 signal: controller ? controller.signal : undefined };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch(url, init).then(function (res) {
      clearTimeout(timer);
      if (res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400)) {
        return { status: 401, redirected: true, data: { code: "auth_required" } };
      }
      var ct = res.headers.get("Content-Type") || "";
      return res.text().then(function (text) {
        var data = null;
        if (ct.indexOf("json") !== -1) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        return { status: res.status, data: data, json: data !== null };
      });
    }, function (err) {
      clearTimeout(timer);
      return { status: 0, network: true, error: err };
    });
  }

  function backoff(attempt) {
    var base = Math.min(30 * 60 * 1000, 5000 * Math.pow(2, Math.max(0, attempt - 1)));
    return base + Math.floor(Math.random() * 1000);
  }

  /** Decide what a server answer means for the record. */
  function classify(res) {
    var d = res.data || {};
    var code = d.code || "";
    if (res.network || res.status === 0) {
      return { action: "retry", stop: "network", error: t("Network problem. Please try again.") };
    }
    if (res.status === 401 || code === "auth_required" || code === "unauthenticated") {
      return { action: "keep", stop: "auth", error: t("Your session has expired. Please log in again.") };
    }
    if (res.status === 402 || code === "subscription_required") {
      return { action: "keep", stop: "subscription", error: d.message || t("Your subscription is not active. Please renew to continue.") };
    }
    if (res.status === 400 && code === "csrf") {
      return { action: "keep", stop: "csrf", error: d.message };
    }
    if (res.status >= 200 && res.status < 300) {
      if (res.json && (d.saved === true || (d.success === true && d.saved !== false))) {
        return { action: "synced" };
      }
      return { action: "retry", error: t("Unexpected answer from the server.") };
    }
    if (res.status === 409) {
      if (code === "tenant_mismatch") { return { action: "blocked", error: d.message }; }
      if (code === "needs_confirmation") {
        return { action: "review", kind: "confirm", error: d.message || d.warning, warnings: d.warnings || (d.warning ? [d.warning] : []) };
      }
      return { action: "review", kind: code || "conflict", error: d.message };
    }
    if (res.status === 429 || res.status >= 500) {
      return { action: "retry", error: d.message || t("Server is busy. Will retry automatically.") };
    }
    // 400, 403, 404, 413, 422: the data itself was rejected. Kept for review.
    return { action: "rejected", kind: code || "rejected", error: d.message || t("Synchronization failed") };
  }

  // ------------------------------------------------------------ sync run
  var running = null;
  function run(options) {
    options = options || {};
    if (running) { return running; }
    running = withLock(function () { return doRun(options); })
      .catch(function (err) {
        notify("error", { error: err && err.message });
        return { error: err && err.message };
      })
      .then(function (r) { running = null; return r; }, function (e) { running = null; throw e; });
    return running;
  }

  function doRun(options) {
    var result = { synced: 0, failed: 0, review: 0, stopped: null, tenant: null };
    return request("/api/session").then(function (ping) {
      if (ping.network) { result.stopped = "offline"; return result; }
      if (ping.status === 401 || !ping.json || !ping.data || !ping.data.tenant_id) {
        result.stopped = "auth";
        notify("auth", {});
        return result;
      }
      var session = ping.data;
      var tenant = String(session.tenant_id);
      result.tenant = tenant;
      if (session.subscription && session.subscription.access === false) {
        result.stopped = "subscription";
        notify("subscription", {});
        return result;
      }
      var csrf = session.csrf_token;
      if (options.tenant && String(options.tenant) !== tenant) {
        // The page belongs to another dairy than the live session (other tab
        // logged in as someone else): do nothing.
        result.stopped = "tenant_changed";
        notify("tenant_changed", { tenant: tenant });
        return result;
      }
      return recoverStale(tenant).then(function () { return forTenant(tenant); }).then(function (rows) {
        var due = rows.filter(function (r) {
          if (r.status === S.PENDING) { return true; }
          if (r.status === S.FAILED && r.retryable && r.retry_count < MAX_AUTO_RETRIES) {
            return (r.next_attempt_at || 0) <= now() || options.manual;
          }
          return false;
        });
        notify("start", { total: due.length, tenant: tenant });
        var i = 0;
        function next() {
          if (i >= due.length) { return Promise.resolve(); }
          var rec = due[i++];
          return update(rec.local_id, { status: S.SYNCING }).then(function () {
            notify("progress", { done: i - 1, total: due.length, record: rec });
            return request(rec.endpoint, {
              method: rec.method, body: rec.payload,
              headers: { "X-CSRFToken": csrf, "X-DP-Tenant": rec.tenant_id,
                         "X-Idempotency-Key": rec.idempotency_key }
            });
          }).then(function (res) {
            var c = classify(res);
            var patch = { last_status: res.status, server: res.data || null };
            if (c.action === "synced") {
              patch.status = S.SYNCED;
              patch.synced_at = now();
              patch.last_error = null;
              result.synced += 1;
            } else if (c.action === "keep") {
              patch.status = S.PENDING;
              patch.last_error = c.error;
              result.stopped = c.stop;
            } else if (c.action === "retry") {
              var n = (rec.retry_count || 0) + (c.stop === "network" ? 0 : 1);
              patch.retry_count = n;
              patch.retryable = true;
              patch.status = c.stop === "network" ? S.PENDING : S.FAILED;
              patch.next_attempt_at = now() + backoff(n || 1);
              patch.last_error = c.error;
              if (c.stop === "network") { result.stopped = "offline"; } else { result.failed += 1; }
            } else if (c.action === "review") {
              patch.status = S.REVIEW;
              patch.review_kind = c.kind;
              patch.warnings = c.warnings || null;
              patch.last_error = c.error;
              result.review += 1;
            } else if (c.action === "blocked") {
              patch.status = S.BLOCKED;
              patch.last_error = c.error;
              result.failed += 1;
            } else {
              patch.status = S.FAILED;
              patch.retryable = false;
              patch.review_kind = c.kind;
              patch.last_error = c.error;
              result.failed += 1;
            }
            return update(rec.local_id, patch).then(function (saved) {
              notify("record", { record: saved, outcome: c.action });
              if (result.stopped) { return null; }
              return next();
            });
          });
        }
        return next().then(function () { return prune(); }).then(function () {
          if (result.stopped === "auth") { notify("auth", {}); }
          if (result.stopped === "csrf") { notify("auth", { csrf: true }); }
          if (result.stopped === "subscription") { notify("subscription", {}); }
          notify("done", { result: result });
          return result;
        });
      });
    });
  }

  function recoverStale(tenant) {
    // A sync interrupted mid-request (tab closed, phone restarted) leaves
    // records in "syncing": put them back in the queue (the idempotency key
    // makes the resend safe).
    return forTenant(tenant).then(function (rows) {
      var stale = rows.filter(function (r) {
        return r.status === S.SYNCING && now() - r.updated_at > STALE_SYNCING_MS;
      });
      return Promise.all(stale.map(function (r) { return update(r.local_id, { status: S.PENDING }); }));
    });
  }
  function prune() {
    return tx("queue", "readwrite", function (s) {
      s.queue.getAll().onsuccess = function (e) {
        (e.target.result || []).forEach(function (r) {
          if (r.status === S.SYNCED && now() - (r.synced_at || r.updated_at) > KEEP_SYNCED_MS) {
            s.queue.delete(r.local_id);
          }
        });
      };
    });
  }

  /** User actions from the review panel. */
  function retry(localId, extraPayload) {
    return tx("queue", "readwrite", function (s) {
      var out = box();
      s.queue.get(localId).onsuccess = function (e) {
        var r = e.target.result;
        if (!r) { return; }
        r.payload = Object.assign({}, r.payload, extraPayload || {});
        r.status = S.PENDING;
        r.retry_count = 0;
        r.next_attempt_at = 0;
        r.legacy = false;
        r.updated_at = now();
        s.queue.put(r);
        out.value = r;
      };
      return out;
    }).then(function (r) { requestSync(); return r; });
  }

  // ------------------------------------------------------------ scheduling
  var timer = null;
  function schedule(ms) {
    if (!IS_WINDOW) { return; }
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (global.navigator.onLine === false || !global.DP_SESSION || !global.DP_SESSION.tenant_id) { return; }
      run({ tenant: global.DP_SESSION.tenant_id });
    }, ms || 0);
  }

  // ------------------------------------------------------------ helper for pages
  /**
   * Save through the network when possible, otherwise on this device.
   * Returns {mode: "online", res} or {mode: "queued", record}.
   * The same idempotency key is used for the online attempt and the queued
   * copy, so "request sent but answer lost" can never create a duplicate.
   */
  function submit(spec) {
    var session = global.DP_SESSION || {};
    var key = spec.idempotency_key || uuid();
    var field = spec.idempotency_field || "client_request_id";
    var payload = Object.assign({}, spec.payload);
    payload[field] = key;
    var queueIt = function (reason) {
      return enqueue({
        type: spec.type, endpoint: spec.endpoint, payload: payload, tenant_id: session.tenant_id,
        role: session.role, staff_id: session.staff_id, idempotency_key: key,
        idempotency_field: field, dedupe_key: spec.dedupe_key, label: spec.label,
        last_error: reason || null
      }).then(function (record) { return { mode: "queued", record: record }; });
    };
    if (!session.tenant_id) {
      return Promise.reject(Object.assign(new Error(t("Your session has expired. Please log in again.")), { code: "auth_required" }));
    }
    if (global.navigator.onLine === false || spec.forceQueue) { return queueIt(null); }
    return request(spec.endpoint, {
      method: "POST", body: payload,
      headers: { "X-CSRFToken": (global.DP && global.DP.csrfToken()) || "",
                 "X-DP-Tenant": String(session.tenant_id), "X-Idempotency-Key": key }
    }).then(function (res) {
      if (res.network || res.status === 0) { return queueIt(t("Network problem. Please try again.")); }
      return { mode: "online", res: res, outcome: classify(res), key: key };
    });
  }

  // ------------------------------------------------------------ legacy queue
  /** Entries saved by the old DairyMitra page carry no dairy id. They are
   *  moved into the new queue FOR REVIEW (never synced automatically, since
   *  they might belong to another account) and removed from the old store
   *  only after the copy has been committed. */
  function migrateLegacy(session) {
    if (!session || !session.tenant_id || !global.indexedDB) { return Promise.resolve(0); }
    return new Promise(function (resolve) {
      var req;
      try { req = global.indexedDB.open(LEGACY_DB); } catch (e) { resolve(0); return; }
      req.onupgradeneeded = function (e) {
        // The old DB does not exist: do not create it.
        e.target.transaction.abort();
      };
      req.onerror = function () { resolve(0); };
      req.onsuccess = function (e) {
        var db = e.target.result;
        if (!db.objectStoreNames.contains("milk_entries")) { db.close(); resolve(0); return; }
        var read = db.transaction("milk_entries", "readonly").objectStore("milk_entries").getAll();
        read.onsuccess = function () {
          var rows = read.result || [];
          if (!rows.length) { db.close(); resolve(0); return; }
          var copies = rows.map(function (r) {
            return enqueue({
              type: "milk", endpoint: "/submit_milk_ajax", tenant_id: session.tenant_id,
              role: session.role, staff_id: session.staff_id, legacy: true, status: S.REVIEW,
              payload: { vendor_id: r.vendor_id, milk_type: r.milk_type, quantity: r.quantity,
                         date: r.date, slot: r.slot },
              label: t("Milk") + " #" + r.vendor_id + " · " + r.date + " · " + r.quantity + " L",
              last_error: t("Saved by the previous app version. Confirm that it belongs to this dairy before syncing.")
            }).then(function () { return r.id; }, function (err) {
              return err && err.code === "duplicate_local" ? r.id : null;
            });
          });
          Promise.all(copies).then(function (ids) {
            var del = db.transaction("milk_entries", "readwrite");
            var store = del.objectStore("milk_entries");
            ids.forEach(function (id) { if (id !== null && id !== undefined) { store.delete(id); } });
            del.oncomplete = function () { db.close(); resolve(ids.filter(function (x) { return x !== null; }).length); };
            del.onerror = function () { db.close(); resolve(0); };
          });
        };
        read.onerror = function () { db.close(); resolve(0); };
      };
    });
  }

  global.DPOffline = {
    STATUS: S, enqueue: enqueue, submit: submit, run: run, retry: retry, remove: remove,
    update: update, forTenant: forTenant, summary: summary, classify: classify,
    requestSync: requestSync, uuid: uuid, migrateLegacy: migrateLegacy, openDB: openDB,
    getMeta: getMeta, setMeta: setMeta, SYNC_TAG: SYNC_TAG
  };

  if (!IS_WINDOW) { return; }

  // ==================================================================
  // Page UI: status pill, review panel, automatic sync triggers
  // ==================================================================
  var DP = global.DP || {};
  function toast(msg, type, opts) { if (DP.toast) { DP.toast(msg, type, opts); } }

  function session() { return global.DP_SESSION || null; }

  function renderPill() {
    var pill = document.getElementById("dp-net-status");
    if (!pill) { return Promise.resolve(); }
    var online = global.navigator.onLine !== false;
    pill.classList.toggle("is-offline", !online);
    var label = pill.querySelector("[data-net-label]");
    if (label) { label.textContent = online ? t("Online") : t("Offline"); }
    var badge = pill.querySelector("[data-net-count]");
    var s = session();
    if (!s || !s.tenant_id) { if (badge) { badge.hidden = true; } return Promise.resolve(); }
    return summary(s.tenant_id).then(function (c) {
      var waiting = c.pending + c.syncing + c.failed + c.review + c.blocked;
      if (badge) {
        badge.hidden = waiting === 0;
        badge.textContent = waiting;
        badge.classList.toggle("is-alert", c.failed + c.review + c.blocked > 0);
      }
      pill.title = waiting ? t("{n} entries waiting for synchronization", { n: waiting })
                           : (online ? t("All entries are synchronized") : t("Offline"));
      return c;
    }).catch(function () {
      if (badge) { badge.hidden = false; badge.textContent = "!"; }
    });
  }

  var STATUS_TEXT = {
    pending: "Waiting for synchronization", syncing: "Synchronizing...",
    synced: "Successfully synchronized", failed: "Synchronization failed",
    needs_review: "Needs your review", blocked: "Belongs to another account"
  };

  function openPanel() {
    var s = session();
    if (!s || !s.tenant_id || !DP.dialog) { return; }
    Promise.all([forTenant(s.tenant_id), summary(s.tenant_id)]).then(function (out) {
      var rows = out[0].slice().reverse();
      var c = out[1];
      var wrap = DP.el("div", { "class": "dp-sync-panel" });
      var online = global.navigator.onLine !== false;
      wrap.appendChild(DP.el("p", { "class": "dp-sync-panel__state",
        text: (online ? t("Online") : t("Offline")) + " · " +
              t("{n} entries waiting for synchronization", { n: c.total_open }) }));
      if (c.other_tenants) {
        wrap.appendChild(DP.el("div", { "class": "dp-note dp-note--warning",
          text: t("{n} entries saved on this device belong to another dairy account. They are kept safely and will synchronise when that account logs in.", { n: c.other_tenants }) }));
      }
      if (!rows.length) {
        wrap.appendChild(DP.el("p", { "class": "dp-muted", text: t("No entries saved on this device.") }));
      }
      var list = DP.el("ul", { "class": "dp-sync-list" });
      rows.slice(0, 200).forEach(function (r) {
        var actions = DP.el("div", { "class": "dp-sync-item__actions" });
        if (r.status === S.REVIEW && r.review_kind === "confirm") {
          actions.appendChild(DP.el("button", { type: "button", "class": "dp-btn dp-btn--primary dp-btn--sm",
            text: t("Confirm and sync"), onclick: function () {
              retry(r.local_id, { force_save: true, confirm_warnings: true }).then(close);
            } }));
        } else if (r.status === S.REVIEW && r.legacy) {
          actions.appendChild(DP.el("button", { type: "button", "class": "dp-btn dp-btn--primary dp-btn--sm",
            text: t("Belongs to this dairy - sync"), onclick: function () {
              retry(r.local_id).then(close);
            } }));
        } else if (r.status === S.FAILED || r.status === S.REVIEW) {
          actions.appendChild(DP.el("button", { type: "button", "class": "dp-btn dp-btn--ghost dp-btn--sm",
            text: t("Retry"), onclick: function () { retry(r.local_id).then(close); } }));
        }
        if (r.status !== S.SYNCED && r.status !== S.SYNCING) {
          actions.appendChild(DP.el("button", { type: "button", "class": "dp-btn dp-btn--danger-ghost dp-btn--sm",
            text: t("Discard"), onclick: function () {
              DP.confirm(t("Discard this entry from this device? It has NOT been saved on the server."),
                         { danger: true, okText: t("Discard") }).then(function (ok) {
                if (ok) { remove(r.local_id).then(close); }
              });
            } }));
        }
        var when = new Date(r.created_at);
        var stamp = ("0" + when.getDate()).slice(-2) + "/" + ("0" + (when.getMonth() + 1)).slice(-2) + "/" +
                    when.getFullYear() + " " + ("0" + when.getHours()).slice(-2) + ":" + ("0" + when.getMinutes()).slice(-2);
        list.appendChild(DP.el("li", { "class": "dp-sync-item dp-sync-item--" + r.status }, [
          DP.el("div", { "class": "dp-sync-item__main" }, [
            DP.el("strong", { text: r.label }),
            DP.el("span", { "class": "dp-sync-item__status", text: t(STATUS_TEXT[r.status] || r.status) }),
            DP.el("small", { text: stamp + (r.retry_count ? " · " + t("Attempts: {n}", { n: r.retry_count }) : "") }),
            r.last_error && r.status !== S.SYNCED ? DP.el("small", { "class": "dp-sync-item__error", text: r.last_error }) : null
          ]),
          actions
        ]));
      });
      wrap.appendChild(list);
      var closeDialog = null;
      function close() { if (closeDialog) { closeDialog("closed"); } refresh(); }
      DP.dialog({ title: t("Offline entries"), message: "", html: wrap, cancel: false,
                  okText: online && c.total_open ? t("Sync now") : t("Close"), icon: "cloud-upload",
                  onOpen: function (fn) { closeDialog = fn; } })
        .then(function (v) { if (v === true && online && c.total_open) { manualSync(); } });
    });
  }

  function manualSync() {
    if (global.navigator.onLine === false) {
      toast(t("This action requires internet."), "offline");
      return;
    }
    toast(t("Synchronizing..."), "sync", { timeout: 2000 });
    var s = session();
    run({ tenant: s && s.tenant_id, manual: true });
  }

  function refresh() { renderPill(); }

  global.addEventListener("dp:sync", function (e) {
    var d = e.detail || {};
    if (d.type === "done" && d.result) {
      var r = d.result;
      if (r.synced) { toast(t("{n} entries successfully synchronized.", { n: r.synced }), "success"); }
      if (r.failed || r.review) {
        toast(t("{n} entries could not be synchronized.", { n: r.failed + r.review }), "error",
              { action: { label: t("Review"), onClick: openPanel }, timeout: 10000 });
      }
    } else if (d.type === "auth") {
      toast(t("Your session has expired. Log in again to synchronize the entries saved on this device."), "warning",
            { timeout: 10000 });
    } else if (d.type === "subscription") {
      toast(t("Entries are saved on this device. Renew the subscription to synchronize them."), "warning");
    }
    refresh();
  });

  if (global.navigator.serviceWorker) {
    global.navigator.serviceWorker.addEventListener("message", function (e) {
      if (e.data && e.data.dpSync) {
        try { global.dispatchEvent(new CustomEvent("dp:sync", { detail: e.data.dpSync })); } catch (x) {}
      }
    });
  }

  global.addEventListener("online", function () {
    refresh();
    var s = session();
    if (!s || !s.tenant_id) { return; }
    summary(s.tenant_id).then(function (c) {
      if (c.pending || c.failed) {
        toast(t("Back online. Synchronizing entries saved on this device..."), "sync", { timeout: 3000 });
        schedule(800);
      } else {
        toast(t("Back online."), "success", { timeout: 2500 });
      }
    }).catch(function () {});
  });
  global.addEventListener("offline", function () {
    toast(t("You are offline. Entries will be saved on this device."), "offline", { timeout: 6000 });
    refresh();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") { return; }
    refresh();
    var s = session();
    if (s && s.tenant_id) {
      summary(s.tenant_id).then(function (c) { if (c.pending || c.failed) { schedule(1000); } }).catch(function () {});
    }
  });
  setInterval(function () {
    var s = session();
    if (!s || !s.tenant_id || global.navigator.onLine === false) { return; }
    summary(s.tenant_id).then(function (c) {
      if (c.pending || (c.failed && c.failed > 0)) { run({ tenant: s.tenant_id }); }
    }).catch(function () {});
  }, 60000);

  // POST forms cannot work without the server: say so instead of letting
  // the browser show its own "no internet" page (and losing the input).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || global.navigator.onLine !== false) { return; }
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") { return; }
    if (form.hasAttribute("data-offline-ok")) { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    toast(t("This action requires internet."), "offline");
  }, true);

  // Logging out never deletes entries that are still waiting; say so.
  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a[data-dp-logout]");
    var s = session();
    if (!link || !s || !s.tenant_id || link.dataset.dpConfirmed === "1") { return; }
    e.preventDefault();
    summary(s.tenant_id).then(function (c) {
      var waiting = c.pending + c.syncing + c.failed + c.review + c.blocked;
      if (!waiting) { global.location.href = link.href; return; }
      DP.confirm(t("{n} entries are saved on this device and not yet synchronized. They will stay safely on this device and synchronize after you log in again. Log out now?", { n: waiting }),
                 { okText: t("Logout"), type: "warning" }).then(function (ok) {
        if (ok) { global.location.href = link.href; }
      });
    }, function () { global.location.href = link.href; });
  });

  function start() {
    var pill = document.getElementById("dp-net-status");
    if (pill) { pill.addEventListener("click", openPanel); }
    var s = session();
    if (s && s.tenant_id) {
      // Tell the service worker which dairy is active (cached pages are
      // kept per dairy).
      if (global.navigator.serviceWorker && global.navigator.serviceWorker.controller) {
        global.navigator.serviceWorker.controller.postMessage({ type: "dp-session", tenant_id: s.tenant_id });
      }
      if (s.role === "owner" || s.role === "staff") {
        migrateLegacy(s).then(function (n) {
          if (n) {
            toast(t("{n} entries from the previous app version need your confirmation.", { n: n }), "warning",
                  { action: { label: t("Review"), onClick: openPanel }, timeout: 12000 });
          }
          refresh();
        });
      }
      // Ping the server only when something is actually waiting here.
      summary(s.tenant_id).then(function (c) {
        if (c.pending || c.failed || c.syncing) { schedule(1200); }
      }).catch(function () {});
    }
    openDB().catch(function () {
      toast(t("This browser cannot store entries offline (private mode or storage blocked)."), "warning",
            { timeout: 10000 });
    });
    refresh();
  }
  global.DPOffline.openPanel = openPanel;
  global.DPOffline.refresh = refresh;
  global.DPOffline.manualSync = manualSync;
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); }
  else { start(); }
})(typeof self !== "undefined" ? self : window);
