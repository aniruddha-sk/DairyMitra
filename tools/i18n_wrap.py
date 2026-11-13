"""
Wrap user-visible template text in {{ _('...') }} so it is translated.

    python tools/i18n_wrap.py --dry-run     # report what would change
    python tools/i18n_wrap.py               # rewrite templates in place

Handled: text between tags (also between Jinja block tags) and the
placeholder / title / aria-label / alt attributes. Never touched: <script>,
<style>, <textarea>, <pre>, <code>, Jinja expressions, other attributes.
Running it again is safe (already wrapped text is inside {{ }} and skipped).
"""
import glob
import html
import json
import re
import sys

LETTERS = re.compile(r"[A-Za-zऀ-ॿ]")
SKIP_BLOCKS = ("script", "style", "textarea", "pre", "code")
WRAP_ATTRS = ("placeholder", "title", "aria-label", "alt")


def jinja_literal(text):
    if "'" not in text and "\\" not in text:
        return "'" + text + "'"
    return json.dumps(text, ensure_ascii=False)


def wrap_text(fragment):
    stripped = fragment.strip()
    if not stripped or not LETTERS.search(stripped):
        return fragment
    lead = fragment[:len(fragment) - len(fragment.lstrip())]
    trail = fragment[len(fragment.rstrip()):]
    text = " ".join(html.unescape(stripped).split())
    return lead + "{{ _(" + jinja_literal(text) + ") }}" + trail


ATTR_RE = re.compile(r'(\s(?:%s)\s*=\s*)(["\'])(.*?)\2' % "|".join(WRAP_ATTRS), re.S)


def wrap_tag(tag):
    def sub(m):
        value = m.group(3)
        if "{{" in value or "{%" in value or not LETTERS.search(value):
            return m.group(0)
        text = " ".join(html.unescape(value).split())
        inner = "{{ _(" + json.dumps(text, ensure_ascii=False) + ") }}" if m.group(2) == "'" else \
            "{{ _(" + ("'" + text + "'" if "'" not in text else json.dumps(text, ensure_ascii=False)) + ") }}"
        if m.group(2) == '"' and '"' in inner and "'" in text:
            return m.group(0)        # cannot nest safely; leave as is
        return m.group(1) + m.group(2) + inner + m.group(2)
    return ATTR_RE.sub(sub, tag)


def process(src):
    out, i, n = [], 0, len(src)
    while i < n:
        lower = src[i:i + 12].lower()
        skipped = False
        for tag in SKIP_BLOCKS:
            if lower.startswith("<" + tag) and (len(src) > i + len(tag) + 1 and not src[i + len(tag) + 1].isalnum()):
                end = src.lower().find("</%s>" % tag, i)
                end = n if end < 0 else end + len(tag) + 3
                out.append(src[i:end])
                i = end
                skipped = True
                break
        if skipped:
            continue
        if src.startswith("<!--", i):
            end = src.find("-->", i)
            end = n if end < 0 else end + 3
            out.append(src[i:end]); i = end; continue
        two = src[i:i + 2]
        if two in ("{{", "{%", "{#"):
            close = {"{{": "}}", "{%": "%}", "{#": "#}"}[two]
            end = src.find(close, i + 2)
            end = n if end < 0 else end + 2
            out.append(src[i:end]); i = end; continue
        if src[i] == "<" and i + 1 < n and (src[i + 1].isalpha() or src[i + 1] in "/!"):
            j, quote = i + 1, None
            while j < n:
                ch = src[j]
                if quote:
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif src.startswith("{{", j) or src.startswith("{%", j):
                    k = src.find("}}" if src.startswith("{{", j) else "%}", j)
                    j = (k + 1) if k > 0 else n
                elif ch == ">":
                    break
                j += 1
            out.append(wrap_tag(src[i:j + 1]))
            i = j + 1
            continue
        j = i
        while j < n and not (src[j] == "<" and j + 1 < n and (src[j + 1].isalpha() or src[j + 1] in "/!")) \
                and src[j:j + 2] not in ("{{", "{%", "{#"):
            j += 1
        if j == i:
            out.append(src[i]); i += 1; continue
        out.append(wrap_text(src[i:j]))
        i = j
    return "".join(out)


def main():
    dry = "--dry-run" in sys.argv
    files = sorted(glob.glob("templates/**/*.html", recursive=True))
    total = 0
    for f in files:
        s = open(f, encoding="utf-8").read()
        new = process(s)
        if new != s:
            added = new.count("{{ _(") - s.count("{{ _(")
            total += added
            print(f"{f}: +{added}")
            if not dry:
                open(f, "w", encoding="utf-8", newline="\n").write(new)
    print("wrapped:", total)


if __name__ == "__main__":
    main()
