import sys, json, httpx
base = "http://127.0.0.1:" + sys.argv[1]
c = httpx.Client(base_url=base)
r = c.post("/api/auth/login", json={"email": "jordan.ellis@example.test", "password": "Demo-Patient-2026!"}); print("login", r.status_code)
for u in sys.argv[2:]:
    r = c.get(u); print("\n##", u, r.status_code); 
    try: print(json.dumps(r.json(), indent=1)[:int(1500)])
    except Exception: print(r.text[:300])
