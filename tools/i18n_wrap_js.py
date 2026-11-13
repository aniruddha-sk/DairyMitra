"""
Wrap Marathi/Hindi string literals in page scripts with t("...").

    python tools/i18n_wrap_js.py --dry-run
    python tools/i18n_wrap_js.py

Covers inline <script> blocks of templates and static/js/*.js (except the
dp-*.js files, which already call t()). Only plain "..." / '...' literals that
contain Devanagari are wrapped. Template literals (`...${x}...`) and literals
used in comparisons are REPORTED for manual review, never changed.
"""
import glob
import re
import sys

DEV = re.compile(r"[ऀ-ॿ]")
LIT = re.compile(r"""(?<![\w$.])(["'])((?:\\.|(?!\1)[^\\\n])*)\1""")
SKIP_FILES = {"dp-ui.js", "dp-icons.js", "dp-date.js", "dp-offline.js", "dp-receipt-print.js",
              "dp-ai.js", "fsn_chart_editor.js", "csrf.js", "service-worker.js"}


def wrap_js(code, report, where):
    out, last = [], 0
    for m in LIT.finditer(code):
        body = m.group(2)
        if not DEV.search(body):
            continue
        before = code[max(0, m.start() - 12):m.start()]
        after = code[m.end():m.end() + 6]
        if re.search(r"\b(t|_|DP\.t)\(\s*$", before):
            continue                                   # already translated
        if re.search(r"(===?|!==?)\s*$", before) or re.match(r"\s*(===?|!==?)", after) \
                or re.search(r"case\s+$", before):
            report.append(f"{where}: comparison literal left as is: {m.group(0)[:60]}")
            continue
        if "{{" in body or "{%" in body:
            report.append(f"{where}: Jinja inside literal left as is: {m.group(0)[:60]}")
            continue
        out.append(code[last:m.start()])
        out.append("t(" + m.group(0) + ")")
        last = m.end()
    out.append(code[last:])
    for tm in re.finditer(r"`[^`]*`", code):
        if DEV.search(tm.group(0)):
            report.append(f"{where}: template literal needs manual t(): {tm.group(0)[:70]!r}")
    return "".join(out)


def main():
    dry = "--dry-run" in sys.argv
    report, changed = [], 0
    for f in sorted(glob.glob("templates/**/*.html", recursive=True)):
        s = open(f, encoding="utf-8").read()

        def repl(m):
            if 'type="application/json"' in m.group(1) or "type='application/json'" in m.group(1):
                return m.group(0)
            return m.group(1) + wrap_js(m.group(2), report, f) + m.group(3)

        new = re.sub(r"(<script\b[^>]*>)(.*?)(</script>)", repl, s, flags=re.S)
        if new != s:
            changed += 1
            print("changed", f)
            if not dry:
                open(f, "w", encoding="utf-8", newline="\n").write(new)
    for f in sorted(glob.glob("static/js/*.js")):
        if f.replace("\\", "/").split("/")[-1] in SKIP_FILES:
            continue
        s = open(f, encoding="utf-8").read()
        new = wrap_js(s, report, f)
        if new != s:
            changed += 1
            print("changed", f)
            if not dry:
                open(f, "w", encoding="utf-8", newline="\n").write(new)
    print("\n".join(report))
    print("files changed:", changed)


if __name__ == "__main__":
    main()
