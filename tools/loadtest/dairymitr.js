// DairyMitra realistic mixed workload (k6).
//   VUS=1000 RUN=4 k6 run dairymitr.js
// Role per VU: 60% collection operators (saves), 25% customers, 15% owners on reports.
// 100 dairies: odd = litre-rate mode, even = FAT/SNF mode (see seed_load.py).
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.BASE || "http://app:8000";
const VUS = parseInt(__ENV.VUS || "100");
const RUN = parseInt(__ENV.RUN || "0");
const HOLD = __ENV.HOLD || "3m";
const RAMP = __ENV.RAMP || "1m";

export const options = {
  noCookiesReset: true,   // keep each virtual user logged in across iterations
  scenarios: {
    mixed: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: RAMP, target: VUS },
        { duration: HOLD, target: VUS },
        { duration: "20s", target: 0 },
      ],
      gracefulRampDown: "30s",
    },
  },
  // per-endpoint thresholds only so the summary export includes each endpoint
  thresholds: {
    "http_req_duration{name:POST /login}": ["p(95)<600000"],
    "http_req_duration{name:GET / (dashboard)}": ["p(95)<600000"],
    "http_req_duration{name:POST /customer/login}": ["p(95)<600000"],
    "http_req_duration{name:POST /api/fat_snf/entries (save)}": ["p(95)<600000"],
    "http_req_duration{name:POST /submit_milk_ajax (save)}": ["p(95)<600000"],
    "http_req_duration{name:GET /api/fat_snf/entries}": ["p(95)<600000"],
    "http_req_duration{name:GET /fat_snf/milk_collection}": ["p(95)<600000"],
    "http_req_duration{name:GET /get_milk_data}": ["p(95)<600000"],
    "http_req_duration{name:GET /milk_collection}": ["p(95)<600000"],
    "http_req_duration{name:GET /fat_snf/calculation 30d}": ["p(95)<600000"],
    "http_req_duration{name:GET /fat_snf/receipt 1 vendor}": ["p(95)<600000"],
    "http_req_duration{name:GET /payment 30d}": ["p(95)<600000"],
    "http_req_duration{name:GET /reports/milk-summary}": ["p(95)<600000"],
    "http_req_duration{name:GET /customer/dashboard}": ["p(95)<600000"],
    "http_req_duration{name:GET /customer/milk-history}": ["p(95)<600000"]
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const saved = new Counter("entries_saved");
const dup = new Counter("entries_duplicate");

const pad = (n, w) => String(n).padStart(w, "0");
function isoDate(base, addDays) {
  const d = new Date(Date.UTC(2025, 0, 2) + (base + addDays) * 86400000);
  return d.toISOString().slice(0, 10);
}
function today() {
  return new Date(Date.now() + 5.5 * 3600000).toISOString().slice(0, 10); // IST
}
function daysAgo(n) {
  return new Date(Date.now() + 5.5 * 3600000 - n * 86400000).toISOString().slice(0, 10);
}
function csrfFromForm(body) {
  const m = body.match(/name="csrf_token"[^>]*value="([^"]+)"/) || body.match(/value="([^"]+)"[^>]*name="csrf_token"/);
  return m ? m[1] : null;
}
function csrfFromMeta(body) {
  const m = body.match(/name="csrf-token" content="([^"]+)"/);
  return m ? m[1] : null;
}

const state = {};

function role() {
  const r = (__VU - 1) % 20;
  return r < 12 ? "operator" : r < 17 ? "customer" : "owner";
}
function dairy() { return ((__VU - 1) % 100) + 1; }
function block() { return Math.floor((__VU - 1) / 100) % 10; }

function ownerLogin() {
  const d = dairy();
  let r = http.get(`${BASE}/login`, { tags: { name: "GET /login" } });
  const token = csrfFromForm(r.body);
  r = http.post(`${BASE}/login`, { csrf_token: token, email: `owner${d}@load.test`, password: "Load@1234" },
    { tags: { name: "POST /login" }, redirects: 1 });
  r = http.get(`${BASE}/`, { tags: { name: "GET / (dashboard)" } });
  state.csrf = csrfFromMeta(r.body);
  // must really land on the dashboard (an unauthenticated GET / redirects to /login,
  // which also carries a csrf meta tag)
  state.ok = check(r, { "owner logged in": (x) => x.status === 200 && !!state.csrf && !x.url.includes("/login") });
}

function customerLogin() {
  const d = dairy();
  const v = (block() * 6) + 1;
  let r = http.get(`${BASE}/customer/login`, { tags: { name: "GET /customer/login" } });
  const token = csrfFromForm(r.body);
  r = http.post(`${BASE}/customer/login`,
    { csrf_token: token, dairy_id: `DM${pad(d, 6)}`, phone: `9${pad(d, 3)}${pad(v, 6)}` },
    { tags: { name: "POST /customer/login" }, redirects: 0 });
  state.ok = check(r, { "customer logged in": (x) => x.status === 302 && !x.headers.Location.includes("login") });
}

function operator(iter) {
  const d = dairy();
  const fsn = d % 2 === 0;
  const vendor = block() * 6 + (iter % 6) + 1;
  const day = isoDate(RUN * 60, Math.floor(iter / 6));   // past, unique per VU+run
  const headers = { "Content-Type": "application/json", "X-CSRFToken": state.csrf, Accept: "application/json" };
  let r;
  if (fsn) {
    r = http.post(`${BASE}/api/fat_snf/entries`, JSON.stringify({
      vendor_id: vendor, milk_type: "cow", fat: "3.5", snf: "8.5", litres: "12.5",
      date: day, slot: "morning", confirm_warnings: true, request_id: `r${RUN}-${__VU}-${iter}`,
    }), { headers, tags: { name: "POST /api/fat_snf/entries (save)" } });
    if (r.status === 201) saved.add(1); else if (r.status === 409) dup.add(1);
    check(r, { "save ok": (x) => x.status === 201 });
    if (iter % 10 === 0) {
      http.get(`${BASE}/api/fat_snf/entries?date=${day}&slot=morning`, { tags: { name: "GET /api/fat_snf/entries" } });
      http.get(`${BASE}/fat_snf/milk_collection`, { tags: { name: "GET /fat_snf/milk_collection" } });
    }
  } else {
    r = http.post(`${BASE}/submit_milk_ajax`, JSON.stringify({
      vendor_id: String(vendor), milk_type: "cow", quantity: "12.5", date: day, slot: "morning", force_save: true,
    }), { headers, tags: { name: "POST /submit_milk_ajax (save)" } });
    if (r.status === 200) saved.add(1); else if (r.status === 409) dup.add(1);
    check(r, { "save ok": (x) => x.status === 200 });
    if (iter % 10 === 0) {
      http.get(`${BASE}/get_milk_data?date=${day}&slot=morning`, { tags: { name: "GET /get_milk_data" } });
      http.get(`${BASE}/milk_collection`, { tags: { name: "GET /milk_collection" } });
    }
  }
  // A real user whose session is gone logs in again.
  if (r.status === 401 || (r.status === 400 && String(r.body).includes('"csrf"'))) state.loggedIn = false;
  sleep(3 + Math.random() * 3);
}

function owner() {
  const d = dairy();
  const from = daysAgo(30), to = daysAgo(1);
  http.get(`${BASE}/`, { tags: { name: "GET / (dashboard)" } });
  let r;
  if (d % 2 === 0) {
    r = http.get(`${BASE}/fat_snf/calculation?from_date=${from}&to_date=${to}`, { tags: { name: "GET /fat_snf/calculation 30d" } });
    check(r, { "report ok": (x) => x.status === 200 });
    r = http.get(`${BASE}/fat_snf/receipt?generate=1&vendor_id=${block() * 6 + 1}&from_date=${from}&to_date=${to}`,
      { tags: { name: "GET /fat_snf/receipt 1 vendor" } });
  } else {
    r = http.get(`${BASE}/payment?start_date=${from}&end_date=${to}`, { tags: { name: "GET /payment 30d" } });
    check(r, { "report ok": (x) => x.status === 200 });
    r = http.get(`${BASE}/reports/milk-summary?date=${to}`, { tags: { name: "GET /reports/milk-summary" } });
  }
  check(r, { "report ok": (x) => x.status === 200 });
  sleep(10 + Math.random() * 10);
}

function customer() {
  let r = http.get(`${BASE}/customer/dashboard`, { tags: { name: "GET /customer/dashboard" } });
  check(r, { "customer page ok": (x) => x.status === 200 });
  r = http.get(`${BASE}/customer/milk-history?from_date=${daysAgo(30)}&to_date=${daysAgo(1)}`,
    { tags: { name: "GET /customer/milk-history" } });
  check(r, { "customer page ok": (x) => x.status === 200 });
  sleep(10 + Math.random() * 10);
}

export default function () {
  if (!state.loggedIn) {
    if (role() === "customer") customerLogin(); else ownerLogin();
    state.loggedIn = true;
    state.iter = 0;
    if (!state.ok) { sleep(5); state.loggedIn = false; return; }
  }
  const it = state.iter++;
  if (role() === "operator") operator(it);
  else if (role() === "owner") owner();
  else customer();
}
