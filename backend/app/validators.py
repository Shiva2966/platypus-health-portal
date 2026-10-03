"""Small shared field validators (W1). Other modules may reuse: F() builds a field spec, clean_value() validates."""
import re
from datetime import date

F = lambda name, label, type="text", required=False, **kw: {"name": name, "label": label, "type": type, "required": required, **kw}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PARTIAL_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,30}$")


def is_date(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        return 1900 <= date.fromisoformat(s).year <= 2100
    except ValueError:
        return False


def clean_value(f: dict, raw):
    """Return cleaned value or raise ValueError(plain-language message)."""
    t = f["type"]
    if t == "checkbox":
        return bool(raw)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if f.get("required"):
            raise ValueError("This field is required.")
        return None
    if t == "number":
        try:
            if isinstance(raw, bool):
                raise ValueError
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Enter a number.")
        if v != v or abs(v) > 1e9:
            raise ValueError("Enter a reasonable number.")
        return v
    if not isinstance(raw, str):
        raise ValueError("Enter text.")
    s = raw.strip()
    if t == "date":
        if not is_date(s):
            raise ValueError("Use a date like 2024-03-15.")
        return s
    if t == "partial_date":
        if not PARTIAL_RE.match(s) or not (1900 <= int(s[:4]) <= 2100):
            raise ValueError("Use a year (2019), year-month (2019-05) or full date (2019-05-17).")
        return s
    if t == "select":
        if s not in f["options"]:
            raise ValueError("Choose one of the options.")
        return s
    limit = 2000 if t == "textarea" else f.get("maxlength", 300)
    if len(s) > limit:
        raise ValueError(f"Too long (max {limit} characters).")
    if t == "email" and not EMAIL_RE.match(s):
        raise ValueError("Enter a valid email address.")
    if t == "tel" and not PHONE_RE.match(s):
        raise ValueError("Enter a phone number using digits, spaces, + ( ) - only.")
    return s
