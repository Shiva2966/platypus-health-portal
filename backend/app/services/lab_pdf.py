"""Heuristic lab-report PDF reader: text out of a PDF (pypdf if installed, else a tiny built-in fallback) and
rows (test, value, unit, reference range, flag) out of the text. Never raises on odd PDFs: returns what it found."""
from __future__ import annotations

import io
import re
import zlib

_NAME_BLOCK = re.compile(r"\b(page|phone|tel|fax|dob|birth|age|sex|patient|mrn|id|npi|acct|account|accession|specimen|"
                         r"address|suite|zip|room|physician|doctor|dr\.|ordered|received|reported|collected|printed|"
                         r"report|reference|range|result|units?|flag|test name|lab director|clia|comment|note)\b", re.I)
_ROW = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z0-9 ,()/%'.\-]{1,58}?)\s{1,}(?P<val>[<>]?\s?\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>(?:[xX]?10[\^E]?\d+/[A-Za-z]+|[A-Za-z%µμ][A-Za-z0-9%µμ/.^*\-]{0,14}))?\s*(?P<rest>.*)$")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)")
_FLAG = re.compile(r"(?:^|\s)(H|L|HI|LO|HIGH|LOW|CRIT(?:ICAL)?\.?|\*)\s*$", re.I)
_DATE = [re.compile(r"(?:collect\w*|date|reported|drawn)[^0-9]{0,25}(\d{4}-\d{2}-\d{2})", re.I),
         re.compile(r"(?:collect\w*|date|reported|drawn)[^0-9]{0,25}(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)]


def _pdf_text_pypdf(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            r.decrypt("")
        return "\n".join((p.extract_text() or "") for p in r.pages)
    except Exception:
        return ""


def _pdf_text_fallback(data: bytes) -> str:
    """Very small extractor: inflates content streams and reads Tj / TJ strings. Works for simple generated PDFs."""
    out: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = m.group(1)
        try:
            raw = zlib.decompress(raw)
        except Exception:
            pass
        if b"BT" not in raw:
            continue
        for line in re.findall(rb"\[(.*?)\]\s*TJ|\((.*?)(?<!\\)\)\s*Tj|T\*|ET", raw, re.S):
            pass
        text = []
        for tm in re.finditer(rb"\[(.*?)\]\s*TJ|\(((?:\\.|[^\\)])*)\)\s*Tj|(ET)|(T\*|Td|TD)", raw, re.S):
            if tm.group(3) or tm.group(4):
                text.append("\n")
            elif tm.group(1) is not None:
                text.append("".join(s.decode("latin-1") for s in re.findall(rb"\(((?:\\.|[^\\)])*)\)", tm.group(1))))
            else:
                text.append(tm.group(2).decode("latin-1"))
        out.append(re.sub(r"\\(.)", r"\1", "".join(text)))
    return "\n".join(out)


def extract_text(data: bytes) -> str:
    t = _pdf_text_pypdf(data)
    if t is None or not t.strip():
        t2 = _pdf_text_fallback(data)
        t = t2 if len(t2.strip()) > len((t or "").strip()) else (t or "")
    return t


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ".").replace("<", "").replace(">", "").replace(" ", ""))
    except ValueError:
        return None


def _find_date(text: str) -> str | None:
    m = _DATE[0].search(text)
    if m:
        return m.group(1)
    m = _DATE[1].search(text)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def parse_rows(text: str) -> list[dict]:
    date = _find_date(text)
    rows, seen = [], set()
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if len(line) < 6 or len(line) > 200:
            continue
        m = _ROW.match(line)
        if not m:
            continue
        name = m.group("name").strip(" .:-")
        if len(name) < 2 or _NAME_BLOCK.search(name) or re.fullmatch(r"[A-Za-z]{1,2}", name):
            continue
        rest = (m.group("rest") or "").strip()
        unit = (m.group("unit") or "").strip()
        flag = None
        fm = _FLAG.search(rest)
        if fm:
            f = fm.group(1).upper()
            flag = "high" if f.startswith("H") else "low" if f.startswith("L") else "critical" if f.startswith("C") else "flagged"
            rest = rest[:fm.start()].strip()
        lo = hi = None
        ref_text = None
        rm = _RANGE.search(rest)
        if rm:
            lo, hi = float(rm.group(1)), float(rm.group(2))
        else:
            lt = re.search(r"[<≤]\s*=?\s*(\d+(?:\.\d+)?)", rest)
            gt = re.search(r"[>≥]\s*=?\s*(\d+(?:\.\d+)?)", rest)
            if lt:
                hi, ref_text = float(lt.group(1)), f"<{lt.group(1)}"
            elif gt:
                lo, ref_text = float(gt.group(1)), f">{gt.group(1)}"
        if not unit and lo is None and hi is None and not ref_text:
            continue  # needs a unit or a range to look like a lab row
        if re.fullmatch(r"\d{2,3}/\d{2,3}", line.split()[-1] if line.split() else ""):
            continue
        val = _num(m.group("val"))
        if val is None:
            continue
        key = (name.lower(), val)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"test_name": name[:200], "value_num": val, "unit": unit[:40] or None, "ref_low": lo, "ref_high": hi,
                     "ref_text": ref_text, "flag": flag, "result_date": date, "category": "lab"})
        if len(rows) >= 200:
            break
    return rows
