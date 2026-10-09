"""
Reproducible load test (stdlib only) for the FAT/SNF hot paths.

    python tools/load_test.py --base http://127.0.0.1:5055 --users 20 --requests 40

Run it ONLY against a demo/staging server seeded with tools/seed_demo.py
(it creates entries). Each virtual user logs in through the real login form
(CSRF included) and then mixes: rate previews, entry lists, saves (each
save is sent twice with the same request_id to prove retries never
duplicate), calculation pages and receipt generation.
"""
import argparse
import http.cookiejar
import json
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, timedelta


class VUser:
    def __init__(self, base, idx):
        self.base = base
        self.idx = idx
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def call(self, method, path, body=None, form=None):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method != "GET" and self.token:
            headers["X-CSRFToken"] = self.token
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        t = time.perf_counter()
        try:
            with self.opener.open(req, timeout=60) as r:
                payload = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
        return status, (time.perf_counter() - t) * 1000, payload

    def login(self, email, password):
        _, _, html = self.call("GET", "/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', html).group(1).decode()
        status, _, _ = self.call("POST", "/login", form={"csrf_token": token, "email": email,
                                                         "password": password})
        _, _, html = self.call("GET", "/settings")
        m = re.search(rb'name="csrf-token" content="([^"]+)"', html)
        self.token = m.group(1).decode() if m else None
        return self.token is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5055")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--requests", type=int, default=40)
    ap.add_argument("--email", default="owner@demo.local")
    ap.add_argument("--password", default="Demo@1234")
    args = ap.parse_args()

    stats = {}
    lock = threading.Lock()
    failures = []
    saves = {"created": 0, "replayed": 0}

    def record(name, status, ms, ok_codes):
        with lock:
            stats.setdefault(name, []).append(ms)
            if status not in ok_codes:
                failures.append((name, status))

    def run(idx):
        u = VUser(args.base, idx)
        if not u.login(args.email, args.password):
            with lock:
                failures.append(("login", 0))
            return
        saves_per_user = (args.requests + 4) // 5
        n_save = 0
        for i in range(args.requests):
            kind = i % 5
            if kind == 0:
                s, ms, _ = u.call("GET", "/api/fat_snf/rate?vendor_id=101&milk_type=cow&fat=3.5&snf=8.5&litres=18")
                record("rate_preview", s, ms, {200})
            elif kind == 1:
                s, ms, _ = u.call("GET", "/api/fat_snf/entries?date=2026-09-25")
                record("entries_list", s, ms, {200})
            elif kind == 2:
                body = {"vendor_id": 104, "milk_type": "cow", "fat": "3.6", "snf": "8.4",
                        "litres": "15",
                        # unique past date per save (charts are effective from 2026-01-01)
                        "date": (date(2026, 1, 1) + timedelta(days=idx * saves_per_user + n_save)).isoformat(),
                        "slot": "morning", "backdated": True, "confirm_warnings": True,
                        "request_id": str(uuid.uuid4())}
                s1, ms, _ = u.call("POST", "/api/fat_snf/entries", body=body)
                record("save_entry", s1, ms, {201})
                s2, ms2, _ = u.call("POST", "/api/fat_snf/entries", body=body)   # retry
                record("save_retry", s2, ms2, {200})
                n_save += 1
                with lock:
                    saves["created"] += s1 == 201
                    saves["replayed"] += s2 == 200
            elif kind == 3:
                s, ms, _ = u.call("GET", "/fat_snf/calculation?from_date=2026-09-21&to_date=2026-09-30")
                record("calculation_page", s, ms, {200})
            else:
                s, ms, _ = u.call("GET", "/fat_snf/receipt?from_date=2026-09-21&to_date=2026-09-30"
                                         "&vendor_id=all&generate=1")
                record("receipt_all", s, ms, {200})

    t0 = time.perf_counter()
    threads = [threading.Thread(target=run, args=(i,)) for i in range(args.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    total = sum(len(v) for v in stats.values())
    print(f"users={args.users} requests={total} wall={wall:.1f}s throughput={total / wall:.1f} req/s")
    print(f"{'endpoint':<18}{'n':>6}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}")
    for name, values in sorted(stats.items()):
        values.sort()
        p95 = values[int(len(values) * 0.95) - 1] if len(values) > 1 else values[0]
        print(f"{name:<18}{len(values):>6}{statistics.median(values):>10.0f}{p95:>10.0f}{values[-1]:>10.0f}")
    print(f"saves created={saves['created']} retries replayed={saves['replayed']}")
    print(f"failures={len(failures)} {failures[:10]}")


if __name__ == "__main__":
    main()
