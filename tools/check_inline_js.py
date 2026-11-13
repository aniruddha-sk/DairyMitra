"""
Syntax-check every inline <script> in the templates and static/js with Node.

    python tools/check_inline_js.py

Jinja expressions are replaced by a placeholder before checking. Exit code 1
when any script has a syntax error. Requires `node` on PATH.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile


def check(code):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(code)
        name = fh.name
    try:
        r = subprocess.run(["node", "--check", name], capture_output=True, text=True)
        return r.returncode == 0, r.stderr
    finally:
        os.unlink(name)


def main():
    bad = 0
    for f in sorted(glob.glob("templates/**/*.html", recursive=True)):
        s = open(f, encoding="utf-8").read()
        for k, m in enumerate(re.finditer(r"<script\b([^>]*)>(.*?)</script>", s, re.S)):
            if "src=" in m.group(1) or "json" in m.group(1):
                continue
            js = re.sub(r"\{\{.*?\}\}", "0", m.group(2), flags=re.S)
            js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
            ok, err = check(js)
            if not ok:
                bad += 1
                print(f"SYNTAX {f} script #{k}: {err.strip().splitlines()[0] if err else ''}")
    for f in sorted(glob.glob("static/js/*.js")) + ["static/service-worker.js"]:
        ok, err = check(open(f, encoding="utf-8").read())
        if not ok:
            bad += 1
            print(f"SYNTAX {f}: {err.strip().splitlines()[0] if err else ''}")
    print("scripts with syntax errors:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
