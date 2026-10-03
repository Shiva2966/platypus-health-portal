"""Probe a running instance (for cron / uptime monitors / deploy smoke tests).

    python scripts/healthcheck.py https://records.example.com          # /healthz + /readyz
    python scripts/healthcheck.py http://127.0.0.1:8000 --ready-only

Exit code 0 = healthy and ready, 1 = not ready / unreachable.  Prints no patient data.
(Note: the sample Caddyfile hides /readyz from the public internet; run this on the host or via the platform.)
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def get(url: str, timeout: float):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--timeout", type=float, default=5)
    ap.add_argument("--ready-only", action="store_true")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    ok = True
    for path in (["/readyz"] if a.ready_only else ["/healthz", "/readyz"]):
        status, body = get(base + path, a.timeout)
        good = status == 200
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'} {path} -> {status}")
        if not good or path == "/readyz":
            try:
                print(json.dumps(json.loads(body), indent=2))
            except Exception:
                print(body[:300])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
