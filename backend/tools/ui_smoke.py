"""Headless-Chrome/Edge UI smoke test via the DevTools protocol (dev tool, needs `pip install websockets`).

    python tools/ui_smoke.py --base http://127.0.0.1:8000 --email jordan.ellis@example.test --password "Demo-Patient-2026!" \
        --routes dashboard,profile,contacts --out data/screens [--mobile]

For each route: loads it signed in, waits, prints the first lines of the page text, console errors / exceptions,
and saves a screenshot. Exit code 1 if any JS exception or console error was seen.
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]


class CDP:
    def __init__(self, ws):
        self.ws, self.n, self.events, self.waiters = ws, 0, [], {}

    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.waiters:
                self.waiters.pop(m["id"]).set_result(m)
            else:
                self.events.append(m)

    async def call(self, method, **params):
        self.n += 1
        fut = asyncio.get_event_loop().create_future()
        self.waiters[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        r = await fut
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    async def eval(self, js, await_promise=True):
        r = await self.call("Runtime.evaluate", expression=js, awaitPromise=await_promise, returnByValue=True)
        return r.get("result", {}).get("value")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--path", default="/", help="page to load, e.g. / or /staff/")
    ap.add_argument("--login-url", default="/api/auth/login")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--routes", default="dashboard")
    ap.add_argument("--out", default="data/screens")
    ap.add_argument("--mobile", action="store_true")
    ap.add_argument("--js", default="", help="extra JS to evaluate and print after each route loads")
    ap.add_argument("--wait", type=float, default=1.5)
    a = ap.parse_args()

    exe = next(p for p in BROWSERS if os.path.exists(p))
    port = 9333
    prof = tempfile.mkdtemp(prefix="hp_ui_")
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                             "--disable-gpu", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            c = CDP(ws)
            pump = asyncio.create_task(c.pump())
            for d in ("Page", "Runtime", "Log", "Network"):
                await c.call(f"{d}.enable")
            if a.mobile:
                await c.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
            await c.call("Page.navigate", url=a.base + a.path)
            await asyncio.sleep(a.wait)
            res = await c.eval(f"fetch('{a.login_url}',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:{json.dumps(a.email)},password:{json.dumps(a.password)}}})}}).then(r=>r.status)")
            print("login status:", res)
            Path(a.out).mkdir(parents=True, exist_ok=True)
            failures = 0
            for route in a.routes.split(","):
                c.events.clear()
                await c.call("Page.navigate", url=f"{a.base}{a.path}#/{route}")
                await asyncio.sleep(0.5)
                await c.call("Page.reload", ignoreCache=True)
                await asyncio.sleep(a.wait + 1)
                text = await c.eval("(document.querySelector('main')||document.body).innerText.slice(0,500)")
                errs = []
                for e in c.events:
                    if e.get("method") == "Runtime.exceptionThrown":
                        errs.append("EXC " + json.dumps(e["params"]["exceptionDetails"].get("exception", {}).get("description", e["params"]["exceptionDetails"].get("text")))[:300])
                    if e.get("method") == "Log.entryAdded" and e["params"]["entry"]["level"] == "error":
                        errs.append("LOG " + e["params"]["entry"]["text"][:200] + " " + e["params"]["entry"].get("url", ""))
                if a.js:
                    print("  js ->", await c.eval(a.js))
                shot = await c.call("Page.captureScreenshot", format="png")
                f = Path(a.out) / f"{route.replace('/', '_')}{'-m' if a.mobile else ''}.png"
                f.write_bytes(base64.b64decode(shot["data"]))
                print(f"\n=== #/{route}  ({f})")
                print((text or "").replace("\n", " | ")[:400])
                for e in errs:
                    print("  !!", e)
                failures += len(errs)
            pump.cancel()
        sys.exit(1 if failures else 0)
    finally:
        proc.terminate()


asyncio.run(main())
