"""python summarize.py <label> : one table per stage from results/."""
import glob
import json
import os
import re
import sys

label = sys.argv[1] if len(sys.argv) > 1 else "default"
here = os.path.dirname(os.path.abspath(__file__))
files = sorted(glob.glob(f"{here}/results/{label}_*.summary.json"),
               key=lambda f: int(re.search(r"_(\d+)\.summary", f).group(1)))


def mem_mib(text):
    v = text.split("/")[0].strip()
    n = float(re.match(r"[\d.]+", v).group())
    return n * 1024 if "GiB" in v else n


for f in files:
    vus = int(re.search(r"_(\d+)\.summary", f).group(1))
    m = json.load(open(f))["metrics"]
    d = m["http_req_duration"]
    fail = m["http_req_failed"]
    checks = m.get("checks", {})
    print(f"\n=== {label}: {vus} virtual users ===")
    print(f"requests {m['http_reqs']['count']}  rate {m['http_reqs']['rate']:.1f}/s  "
          f"failed {fail.get('value', fail.get('rate', 0)) * 100:.2f}%  "
          f"checks {checks.get('value', 0) * 100:.2f}%  "
          f"saved {m.get('entries_saved', {}).get('count', 0)}  dup {m.get('entries_duplicate', {}).get('count', 0)}")
    print(f"all requests  p50 {d['med']:.0f} ms  p95 {d['p(95)']:.0f} ms  p99 {d['p(99)']:.0f} ms  max {d['max']:.0f} ms")
    rows = [(k[len("http_req_duration{name:"):-1], v) for k, v in m.items()
            if k.startswith("http_req_duration{name:") and v.get("max", 0) > 0]
    for name, v in sorted(rows, key=lambda r: -r[1]["p(95)"]):
        print(f"  {name:36s} p50 {v['med']:7.0f}  p95 {v['p(95)']:7.0f}  p99 {v['p(99)']:7.0f}  max {v['max']:7.0f}")
    stats = open(f.replace(".summary.json", ".stats.csv")).read().splitlines()
    app = [s.split(",") for s in stats if s.startswith("dmload-app-1")]
    db = [s.split(",") for s in stats if s.startswith("dmload-db-1")]
    conns = [int(s.split(",")[1]) for s in stats if s.startswith("conns,") and s.split(",")[1].isdigit()]
    if app:
        print(f"  app CPU peak {max(float(a[1].rstrip('%')) for a in app):.0f}% of 200%  "
              f"mem peak {max(mem_mib(a[2]) for a in app):.0f} MiB")
    if db:
        print(f"  db  CPU peak {max(float(a[1].rstrip('%')) for a in db):.0f}% of 200%  "
              f"mem peak {max(mem_mib(a[2]) for a in db):.0f} MiB")
    if conns:
        print(f"  MySQL threads_connected peak {max(conns)}")
    print("  " + open(f.replace(".summary.json", ".db.txt")).read().replace("\n", "  "))
