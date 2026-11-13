/* =====================================================================
   DairyPro service worker
   - App shell (CSS, JS, icons, fonts, translations, offline page) is
     pre-cached, so the app opens without internet after the first visit.
   - Pages: network first; a copy of every successfully loaded page is kept
     PER ACCOUNT (owner / staff / customer scope from the X-DP-Cache-Scope
     header), so offline navigation shows only the logged-in account's own
     pages. Logging out removes that account's page copies.
   - Data APIs are never cached (financial data must be live).
   - Background Sync runs the same sync engine as the pages (dp-offline.js).
   ===================================================================== */
"use strict";

const VERSION = "2026.11.13-1";
const SHELL_CACHE = "dp-shell-" + VERSION;
const RUNTIME_CACHE = "dp-runtime-v1";
const PAGE_CACHE_PREFIX = "dp-pages-";

importScripts("/static/js/dp-offline.js?v=" + VERSION);

const SHELL_REQUIRED = [
  "/offline",
  "/static/css/style.css",
  "/static/css/dairypro.css",
  "/static/js/dp-ui.js",
  "/static/js/dp-icons.js",
  "/static/js/dp-date.js",
  "/static/js/dp-offline.js",
  "/static/js/csrf.js",
  "/static/icons/dp-icons.svg",
  "/static/images/dairypro-logo.png",
  "/static/images/dairypro-icon.png",
  "/static/icons/icon-192.png",
  "/static/manifest.json"
];
const SHELL_OPTIONAL = [
  "/static/css/receipt.css",
  "/static/css/about.css",
  "/static/css/fat_snf.css",
  "/static/css/auth.css",
  "/static/css/security.css",
  "/static/vendor/bootstrap-5.3.3.min.css",
  "/static/js/fsn_common.js",
  "/static/js/fsn_chart_editor.js",
  "/static/js/dp-receipt-print.js",
  "/static/js/subscribe.js",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
  "/i18n/mr.js",
  "/i18n/en.js",
  "/i18n/hi.js"
];

// Never stored as offline page copies.
const NO_PAGE_CACHE = [
  /^\/logout/, /^\/customer\/logout/, /^\/login/, /^\/customer\/login/, /^\/signup/,
  /^\/verify/, /^\/forgot/, /^\/reset/, /^\/subscription/, /^\/admin/, /^\/razorpay/,
  /^\/set-language/, /^\/backup/, /^\/restore/, /^\/delete_account/, /^\/api\//,
  /^\/healthcheck/, /^\/offline/
];

/* ------------------------------------------------------------ install */
self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await cache.addAll(SHELL_REQUIRED.map((u) => new Request(u, { cache: "reload" })));
    await Promise.allSettled(SHELL_OPTIONAL.map((u) =>
      cache.add(new Request(u, { cache: "reload" }))));
    await self.skipWaiting();
  })());
});

/* ------------------------------------------------------------ activate */
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((key) => {
      if ((key.startsWith("dp-shell-") && key !== SHELL_CACHE) || key.startsWith("dairy-mitr-cache-")) {
        return caches.delete(key);
      }
      return null;
    }));
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) { /* optional */ }
    }
    await self.clients.claim();
  })());
});

/* ------------------------------------------------------------ helpers */
async function getScope() {
  try { return (await self.DPOffline.getMeta("page_scope")) || null; }
  catch (e) { return null; }
}
async function setScope(scope) {
  try { await self.DPOffline.setMeta("page_scope", scope || null); } catch (e) { /* ignore */ }
}
function pageCacheName(scope) {
  return PAGE_CACHE_PREFIX + String(scope).replace(/[^A-Za-z0-9_-]/g, "");
}
function cacheablePath(path) {
  return !NO_PAGE_CACHE.some((re) => re.test(path));
}
async function storePage(scope, url, response) {
  const body = await response.clone().blob();
  const headers = new Headers(response.headers);
  headers.set("X-DP-Cached-At", new Date().toISOString());
  headers.delete("Set-Cookie");
  const copy = new Response(body, { status: 200, statusText: "OK", headers });
  const cache = await caches.open(pageCacheName(scope));
  await cache.put(url, copy);
}
async function markOffline(response) {
  // Inject a marker so the page can show "offline copy saved on ...".
  const cachedAt = response.headers.get("X-DP-Cached-At") || "";
  const html = await response.text();
  const marker = '<meta name="dp-offline-copy" content="' + cachedAt + '">';
  const out = html.replace(/<head([^>]*)>/i, (m) => m + marker);
  const headers = new Headers(response.headers);
  headers.set("Content-Type", "text/html; charset=utf-8");
  return new Response(out, { status: 200, headers });
}
async function offlineFallback() {
  const shell = await caches.open(SHELL_CACHE);
  return (await shell.match("/offline")) ||
    new Response("<h1>Offline</h1><p>DairyPro needs an internet connection the first time it is opened.</p>",
                 { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

/* ------------------------------------------------------------ navigation */
async function handleNavigation(event) {
  const request = event.request;
  const url = new URL(request.url);
  try {
    const preload = event.preloadResponse ? await event.preloadResponse : null;
    const response = preload || await fetch(request);
    const scope = response.headers.get("X-DP-Cache-Scope");
    const finalPath = new URL(response.url || request.url).pathname;
    if (/^\/(customer\/)?login/.test(finalPath) || url.pathname.startsWith("/logout")
        || url.pathname.startsWith("/customer/logout")) {
      // Logged out (or session expired): forget the account's page copies.
      const old = await getScope();
      if (old) { await caches.delete(pageCacheName(old)); }
      await setScope(null);
    } else if (scope) {
      const old = await getScope();
      if (old && old !== scope) { await caches.delete(pageCacheName(old)); }
      if (old !== scope) { await setScope(scope); }
      const ct = response.headers.get("Content-Type") || "";
      if (response.ok && ct.includes("text/html") && cacheablePath(url.pathname)) {
        event.waitUntil(storePage(scope, request.url, response.clone()).then(() =>
          (response.redirected && response.url !== request.url && cacheablePath(finalPath))
            ? storePage(scope, response.url, response.clone()) : null).catch(() => null));
      }
    }
    return response;
  } catch (err) {
    const scope = await getScope();
    if (scope) {
      const cache = await caches.open(pageCacheName(scope));
      const exact = await cache.match(request.url);
      if (exact) { return markOffline(exact); }
      const samePath = await cache.match(request.url, { ignoreSearch: true });
      if (samePath) { return markOffline(samePath); }
    }
    return offlineFallback();
  }
}

/* ------------------------------------------------------------ static */
async function staleWhileRevalidate(event, cacheName) {
  const request = event.request;
  const cached = await caches.match(request, { ignoreSearch: true });
  const network = fetch(request).then(async (response) => {
    if (response && (response.ok || response.type === "opaque")) {
      const cache = await caches.open(cacheName);
      await cache.put(request, response.clone());
    }
    return response;
  }).catch(() => null);
  if (cached) {
    event.waitUntil(network);
    return cached;
  }
  const fresh = await network;
  return fresh || new Response("", { status: 504, statusText: "Offline" });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") { return; }
  const url = new URL(request.url);

  if (url.origin !== self.location.origin) {
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
      event.respondWith(staleWhileRevalidate(event, RUNTIME_CACHE));
    }
    return;   // Razorpay etc.: browser default
  }
  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(event));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/i18n/")
      || url.pathname === "/manifest.json" || url.pathname === "/favicon.ico") {
    event.respondWith(staleWhileRevalidate(event, RUNTIME_CACHE));
  }
  // Everything else (APIs, JSON, exports): network only.
});

/* ------------------------------------------------------------ messages */
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "dp-warm" && Array.isArray(data.urls)) {
    event.waitUntil(warm(data.urls));
  } else if (data.type === "dp-sync-now") {
    event.waitUntil(self.DPOffline.run({ source: "sw-message" }));
  } else if (data.type === "skip-waiting") {
    self.skipWaiting();
  }
});

async function warm(urls) {
  // Pre-load the main pages of the logged-in account for offline use.
  // Throttled to once every 6 hours per account to keep server load low.
  const scope = await getScope();
  if (!scope) { return; }
  const key = "warm_" + scope;
  const last = await self.DPOffline.getMeta(key).catch(() => 0);
  if (last && Date.now() - last < 6 * 3600 * 1000) { return; }
  for (const u of urls.slice(0, 10)) {
    try {
      const url = new URL(u, self.location.origin);
      if (url.origin !== self.location.origin || !cacheablePath(url.pathname)) { continue; }
      const response = await fetch(url.href, { credentials: "same-origin", redirect: "follow" });
      const respScope = response.headers.get("X-DP-Cache-Scope");
      const ct = response.headers.get("Content-Type") || "";
      if (response.ok && respScope === scope && ct.includes("text/html")) {
        await storePage(scope, url.href, response);
      }
    } catch (e) { /* offline or failed: skip */ }
  }
  await self.DPOffline.setMeta(key, Date.now()).catch(() => null);
}

/* ------------------------------------------------------------ background sync */
self.addEventListener("sync", (event) => {
  if (event.tag === self.DPOffline.SYNC_TAG) {
    event.waitUntil(self.DPOffline.run({ source: "background-sync" }).then((result) => {
      // Rejecting makes the browser retry later when work was left over.
      if (result && (result.stopped === "offline" || result.stopped === "network")) {
        throw new Error("still offline");
      }
    }));
  }
});

/* ------------------------------------------------------------ push */
self.addEventListener("push", (event) => {
  if (!event.data) { return; }
  let data;
  try { data = event.data.json(); }
  catch (e) { data = { title: "DairyPro", body: event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "DairyPro", {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    vibrate: [200, 100, 200],
    data: { url: data.url || "/customer/dashboard" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/customer/dashboard";
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const client of list) {
      if (client.url.includes(url)) { return client.focus(); }
    }
    return clients.openWindow(url);
  }));
});
