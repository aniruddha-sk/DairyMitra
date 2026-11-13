"""
Turn user-facing f-strings in app.py into translatable _t() calls:

    f"FAT {f} / SNF {s} दोनदा आले आहे."
 -> _t("FAT {f} / SNF {s} दोनदा आले आहे.", f=f, s=s)

The catalog key keeps named placeholders, so every language can place the
values where its grammar needs them. Only the line numbers passed on the
command line are converted (review the list first with --list).

    python tools/i18n_fstrings.py --list
    python tools/i18n_fstrings.py 1973 2619 ...
"""
import ast
import json
import re
import sys

SRC = "app.py"
USER_CALLS = ("ValidationError", "RateNotFound", "Conflict", "NotFound", "Forbidden", "flash",
              "_advance_json_error", "json_error", "append")


def placeholder_name(expr_src, used):
    base = re.sub(r"\W+", "_", expr_src).strip("_").lower() or "value"
    if re.fullmatch(r"[a-z_][a-z0-9_]*", base) is None:
        base = "v_" + base
    base = base[:30]
    name, i = base, 2
    while name in used and used[name] != expr_src:
        name, i = f"{base}{i}", i + 1
    used[name] = expr_src
    return name


def convert(node, src):
    used = {}
    template, params = [], []
    for part in node.values:
        if isinstance(part, ast.Constant):
            template.append(part.value.replace("{", "{{").replace("}", "}}"))
        else:
            expr = ast.get_source_segment(src, part.value)
            name = placeholder_name(expr, used)
            spec = ""
            if part.format_spec is not None:
                spec = ":" + "".join(v.value for v in part.format_spec.values if isinstance(v, ast.Constant))
            conv = "" if part.conversion == -1 else "!" + chr(part.conversion)
            template.append("{" + name + conv + spec + "}")
            if (name, expr) not in params:
                params.append((name, expr))
    literal = json.dumps("".join(template), ensure_ascii=False)
    args = "".join(f", {n}={e}" for n, e in params)
    return f"_t({literal}{args})"


def main():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    if "--list" in sys.argv:
        for n in sorted(nodes, key=lambda n: n.lineno):
            print(n.lineno, ast.get_source_segment(src, n)[:100].replace("\n", " "))
        return
    wanted = {int(a) for a in sys.argv[1:]}
    # also accept implicit concatenations: "plain " f"..." -> parent is the
    # whole string expression; find the JoinedStr that starts on each line
    targets = [n for n in nodes if n.lineno in wanted]
    lines = src.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def pos(lineno, col):
        return offsets[lineno - 1] + len(lines[lineno - 1].encode("utf-8")[:col].decode("utf-8"))

    edits = []
    for n in targets:
        start, end = pos(n.lineno, n.col_offset), pos(n.end_lineno, n.end_col_offset)
        edits.append((start, end, convert(n, src)))
    for start, end, text in sorted(edits, reverse=True):
        src = src[:start] + text + src[end:]
    open(SRC, "w", encoding="utf-8", newline="\n").write(src)
    print("converted", len(edits))


if __name__ == "__main__":
    main()
