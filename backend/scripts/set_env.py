"""Set keys in .env without touching any other byte (values of other keys, comments, line endings).

    .venv\\Scripts\\python.exe scripts\\set_env.py APP_ENV=production DEV_SHOW_OTP=0
    .venv\\Scripts\\python.exe scripts\\set_env.py --generate-secret-key      (only if SECRET_KEY is missing/empty)
    .venv\\Scripts\\python.exe scripts\\set_env.py --generate=DATA_ENCRYPTION_KEY --generate=READYZ_TOKEN   (only if empty)

Prints key NAMES only, never values. Refuses to edit SMTP_PASSWORD.
"""
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.settings import write_env_key  # noqa: E402

ENV = ROOT / ".env"
PROTECTED = {"SMTP_PASSWORD"}


def _strip(data: bytes, keys: set[str]) -> bytes:
    """The file minus the lines for `keys` (used to prove nothing else changed)."""
    out = []
    for line in data.splitlines(keepends=True):
        m = re.match(rb"[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", line)
        if m and m.group(1).decode() in keys:
            continue
        out.append(line)
    joined = b"".join(out)
    return joined.rstrip(b"\r\n")


def _current(data: bytes, key: str) -> str | None:
    m = re.search(rb"^[ \t]*" + re.escape(key.encode()) + rb"[ \t]*=(.*?)(?=\r?\n|\Z)", data, re.M)
    return None if m is None else m.group(1).decode(errors="replace").strip()


def main(argv: list[str]) -> int:
    pairs: dict[str, str] = {}
    for arg in argv:
        if arg == "--generate-secret-key":
            data = ENV.read_bytes() if ENV.exists() else b""
            if _current(data, "SECRET_KEY"):
                print("SECRET_KEY already set - left unchanged.")
            else:
                pairs["SECRET_KEY"] = secrets.token_urlsafe(48)
            continue
        if arg.startswith("--generate="):  # 32 random bytes, urlsafe base64 (AES-256 key / token); only if empty
            name = arg.split("=", 1)[1]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in PROTECTED:
                print(f"Bad key name: {name!r}")
                return 2
            data = ENV.read_bytes() if ENV.exists() else b""
            if _current(data, name):
                print(f"{name} already set - left unchanged.")
            else:
                pairs[name] = secrets.token_urlsafe(32)
            continue
        key, sep, value = arg.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"Bad argument (want KEY=VALUE): {key!r}")
            return 2
        if key in PROTECTED:
            print(f"Refusing to edit {key}.")
            return 2
        pairs[key] = value
    if not pairs:
        print("Nothing to change.")
        return 0
    before = ENV.read_bytes() if ENV.exists() else b""
    tmp = ENV.with_name(".env.tmp-edit")
    tmp.write_bytes(before)
    try:
        for k, v in pairs.items():
            write_env_key(tmp, k, v)
        after = tmp.read_bytes()
        if _strip(before, set(pairs)) != _strip(after, set(pairs)):
            print("Safety check failed: other lines would change. .env left untouched.")
            return 1
        for k, v in pairs.items():
            if _current(after, k) != v.strip():
                print(f"Safety check failed for {k}. .env left untouched.")
                return 1
        ENV.write_bytes(after)
    finally:
        tmp.unlink(missing_ok=True)
    print("Updated .env keys: " + ", ".join(pairs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
