"""QA static checks on the vanilla-JS frontend.

Native DOM ``node.append(a, b, ...)`` turns ``null`` into the visible text "null" (unlike ``Portal.el``, which
skips null/undefined/false children). These tests flag top-level ``.append(...)`` arguments that can be null.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
JS_FILES = sorted(p for p in STATIC.rglob("*.js") if p.name != "sw.js")
NULLABLE_HELPERS = ("recordLines(",)


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.S)
    return re.sub(r"(?m)(^|[^:\\'\"])//[^\n]*", lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), src)


def _call_args(src: str, open_idx: int) -> list[str]:
    """Split the argument list starting right after '(' at open_idx into top-level args."""
    depth, i, start, args, quote = 0, open_idx + 1, open_idx + 1, [], None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                args.append(src[start:i])
                return args
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(src[start:i])
            start = i + 1
        i += 1
    return args


def _top_level_ternary_else_null(arg: str) -> bool:
    depth, quote, i = 0, None, 0
    last_colon = -1
    seen_q = False
    while i < len(arg):
        ch = arg[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "?" and arg[i + 1:i + 2] not in (".", "?"):
            seen_q = True
        elif depth == 0 and ch == ":" and seen_q:
            last_colon = i
        i += 1
    if not seen_q or last_colon < 0:
        return False
    stripped = arg.strip()
    return bool(re.search(r"\?\s*null\s*:", stripped)) or arg[last_colon + 1:].strip() in ("null", "undefined")


def _nullable_appends(src: str):
    src = _strip_comments(src)
    for m in re.finditer(r"\.append\(", src):
        for arg in _call_args(src, m.end() - 1):
            a = arg.strip()
            bad = _top_level_ternary_else_null(a) or (
                any(a.startswith(h) or ("." + h) in a.split("(")[0] + "(" for h in NULLABLE_HELPERS) and "||" not in a)
            if bad:
                yield src.count("\n", 0, m.start()) + 1, a[:120]


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: str(p.relative_to(STATIC)))
def test_native_append_never_gets_null(path):
    hits = list(_nullable_appends(path.read_text(encoding="utf-8")))
    assert not hits, f"{path.name}: native .append() may receive null (renders the text 'null'): {hits}"


def test_detector_catches_the_original_bug():
    bad = 'body.append(field("x", a), cond ? el("label", {}, "y") : null, status);\nbody.append(DocsUI.recordLines(pv.records));'
    good = 'body.append(cond ? el("p") : "", DocsUI.recordLines(pv.records) || "");\nel("div", {}, cond ? x : null);'
    assert len(list(_nullable_appends(bad))) == 2
    assert list(_nullable_appends(good)) == []
