"""
Collect every translatable string and keep i18n/catalog.json in sync.

    python tools/i18n_extract.py --report      # counts + missing translations
    python tools/i18n_extract.py --update      # add new keys (existing ones kept)
    python tools/i18n_extract.py --missing out.json   # dump entries lacking a language

Sources
  templates:  {{ _('...') }}  and  _("...")  inside Jinja expressions
  app.py:     _t("...") calls, and literal messages passed to flash(),
              json_error(), ValidationError(), Conflict(), NotFound(),
              Forbidden(), RateNotFound(), _advance_json_error(), and the
              "message"/"error"/"warning" values of jsonify({...})
  JavaScript: t("...") in static/js and inline <script> blocks
Strings found in JavaScript are flagged "js": true so they are shipped in
/i18n/<lang>.js.
"""
import ast
import glob
import json
import re
import sys
from collections import OrderedDict

CATALOG = "i18n/catalog.json"
DEV = re.compile(r"[ऀ-ॿ]")
LANGS = ("mr", "en", "hi")
MESSAGE_CALLS = {"flash", "json_error", "ValidationError", "Conflict", "NotFound", "Forbidden",
                 "RateNotFound", "_advance_json_error", "_t", "RazorpayError"}
EXTRA_KEYS = [
    "Monthly", "Six Months", "Yearly",
    "payment_status_created", "payment_status_attempted", "payment_status_pending",
    "payment_status_paid", "payment_status_failed", "payment_status_cancelled",
    "payment_status_refunded", "payment_status_partially_refunded",
]


def norm(s):
    return " ".join(str(s).split())


def is_noise(k):
    """Codes, field names and patterns that are never shown as text."""
    if re.fullmatch(r"[a-z][a-z0-9_]*", k) and not re.fullmatch(r"payment_status_[a-z_]+", k):
        return True
    if k in ("payment_status_",) or re.search(r"\[[^\]]*-[^\]]*\]", k):
        return True
    return False


def template_keys():
    keys = set()
    pat = re.compile(r"""\b_\(\s*(?P<lit>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
    for f in glob.glob("templates/**/*.html", recursive=True):
        for m in pat.finditer(open(f, encoding="utf-8").read()):
            try:
                keys.add(norm(ast.literal_eval(m.group("lit"))))
            except (SyntaxError, ValueError):
                pass
    return keys


def python_keys():
    src = open("app.py", encoding="utf-8").read()
    tree = ast.parse(src)
    keys = set()

    def add(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = norm(node.value)
            if v and len(v) < 400 and re.search(r"[A-Za-zऀ-ॿ]", v):
                keys.add(v)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            if name in MESSAGE_CALLS:
                for a in node.args:
                    add(a)
                    if isinstance(a, ast.IfExp):
                        add(a.body); add(a.orelse)
            if name == "jsonify" and node.args and isinstance(node.args[0], ast.Dict):
                for k, v in zip(node.args[0].keys, node.args[0].values):
                    if isinstance(k, ast.Constant) and k.value in ("message", "error", "warning"):
                        add(v)
                        if isinstance(v, ast.IfExp):
                            add(v.body); add(v.orelse)
        # module-level message constants and every Devanagari literal
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.endswith(("_MESSAGE", "_MSG"))
                                                for t in node.targets):
            add(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and DEV.search(node.value) \
                and len(node.value) < 400:
            add(node)
        if isinstance(node, ast.Dict):            # dict literals of messages
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str) and DEV.search(v.value):
                    add(v)
    # strings passed as `msg = "..."` that later go through flash/json are covered by DEV scan;
    # English ones used via variables:
    for m in re.finditer(r'msg\s*=\s*\(?\s*"([^"]+)"', src):
        keys.add(norm(m.group(1)))
    return keys


def js_sources():
    out = []
    for f in glob.glob("static/js/*.js"):
        out.append(open(f, encoding="utf-8").read())
    for f in glob.glob("templates/**/*.html", recursive=True):
        s = open(f, encoding="utf-8").read()
        out.extend(m.group(1) for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", s, re.S))
    return "\n".join(out)


def js_keys(js):
    keys = set()
    for m in re.finditer(r"""\bt\(\s*(?P<lit>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""", js):
        try:
            keys.add(norm(json.loads(m.group("lit")) if m.group("lit")[0] == '"' else ast.literal_eval(m.group("lit"))))
        except (ValueError, SyntaxError):
            pass
    return keys


def js_literals(js):
    lits = set()
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'', js):
        lits.add(norm(m.group(1) if m.group(1) is not None else m.group(2)))
    return lits


def load():
    try:
        return json.load(open(CATALOG, encoding="utf-8"))
    except FileNotFoundError:
        return []


def main():
    entries = load()
    index = {}
    for e in entries:
        for lang in LANGS:
            if e.get(lang):
                index.setdefault(norm(e[lang]), e)
    js = js_sources()
    jkeys = js_keys(js)
    all_keys = {k for k in template_keys() | python_keys() | jkeys | set(EXTRA_KEYS) if not is_noise(k)}
    lits = js_literals(js)
    new = []
    for k in sorted(all_keys):
        if k in index:
            continue
        src_lang = "mr" if DEV.search(k) else "en"
        e = OrderedDict([("mr", None), ("en", None), ("hi", None)])
        e[src_lang] = k
        new.append(e)
        index[k] = e
    # translations written by hand: i18n/sources/en_*.json -> {english: [mr, hi]},
    # i18n/sources/mr_*.json -> {marathi: [en, hi]}
    unmatched = []
    for src in sorted(glob.glob("i18n/sources/*.json")):
        kind = "en" if src.replace("\\", "/").split("/")[-1].startswith("en_") else "mr"
        others = ("mr", "hi") if kind == "en" else ("en", "hi")
        for key, values in json.load(open(src, encoding="utf-8")).items():
            # match on the SOURCE language first ("बंद" may also be the Marathi
            # translation of "Disabled"; it must still get its own entry filled)
            same = [x for x in entries + new if x.get(kind) and norm(x[kind]) == norm(key)]
            for x in same:
                for lang, value in zip(others, values):
                    if value and not x.get(lang):
                        x[lang] = value
            e = same[0] if same else index.get(norm(key))
            if e is None:
                if kind == "mr" and not DEV.search(key):
                    continue
                e = OrderedDict([("mr", None), ("en", None), ("hi", None)])
                e[kind] = key
                (new if "--update" in sys.argv else []).append(e)
                index[norm(key)] = e
                if norm(key) not in all_keys:
                    unmatched.append(key)
            for lang, value in zip(others, values):
                if value and not e.get(lang):
                    e[lang] = value
    for e in entries + new:
        variants = {norm(e[l]) for l in LANGS if e.get(l)}
        e["js"] = bool(variants & (jkeys | lits))
    if unmatched and ("--report" in sys.argv or len(sys.argv) == 1):
        print("source keys not used anywhere (kept):", len(unmatched))
        for k in unmatched[:40]:
            print("   ", k)
    missing = [e for e in entries + new if not all(e.get(l) for l in LANGS)]
    if "--report" in sys.argv or len(sys.argv) == 1:
        print(f"keys in use: {len(all_keys)}  catalog entries: {len(entries)}  new: {len(new)}  "
              f"incomplete: {len(missing)}  js: {sum(1 for e in entries + new if e['js'])}")
    if "--update" in sys.argv:
        json.dump(entries + new, open(CATALOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("catalog updated:", len(entries + new))
    if "--missing" in sys.argv:
        out = sys.argv[sys.argv.index("--missing") + 1]
        json.dump(missing, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("missing written:", out, len(missing))
    if "--unused" in sys.argv:
        for e in entries:
            if not any(norm(e[l]) in all_keys for l in LANGS if e.get(l)):
                print("unused:", e.get("en") or e.get("mr"))


if __name__ == "__main__":
    main()
