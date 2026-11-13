"""
Wrap Devanagari string literals that sit INSIDE Jinja expressions:

    {{ 'सक्रिय' if c.status == 'active' else 'निष्क्रिय' }}
 -> {{ _('सक्रिय') if c.status == 'active' else _('निष्क्रिय') }}

Literals already passed to _() and literals used in comparisons are left alone.

    python tools/i18n_wrap_jinja_literals.py [--dry-run]
"""
import glob
import re
import sys

DEV = re.compile(r"[ऀ-ॿ]")
EXPR = re.compile(r"(\{\{|\{%)(.*?)(\}\}|%\})", re.S)
LIT = re.compile(r"""(["'])((?:(?!\1).)*?)\1""")


def wrap_expr(body):
    out, last = [], 0
    for m in LIT.finditer(body):
        if not DEV.search(m.group(2)):
            continue
        before = body[max(0, m.start() - 6):m.start()]
        after = body[m.end():m.end() + 4]
        if re.search(r"_\(\s*$", before) or re.search(r"(==|!=|\bin)\s*$", before) \
                or re.match(r"\s*(==|!=|:)", after):
            continue
        out.append(body[last:m.start()])
        out.append("_(" + m.group(0) + ")")
        last = m.end()
    out.append(body[last:])
    return "".join(out)


def main():
    dry = "--dry-run" in sys.argv
    for f in sorted(glob.glob("templates/**/*.html", recursive=True)):
        s = open(f, encoding="utf-8").read()
        new = EXPR.sub(lambda m: m.group(1) + wrap_expr(m.group(2)) + m.group(3), s)
        if new != s:
            print("changed", f)
            if not dry:
                open(f, "w", encoding="utf-8", newline="\n").write(new)


if __name__ == "__main__":
    main()
