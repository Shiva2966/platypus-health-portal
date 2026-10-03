# Platypus

Patient-controlled health portal with a FastAPI backend, patient web app,
hospital staff web app, Android patient client and Windows hospital client.

## Submission contents
- `backend/`: source, requirements, tests, migrations and deployment documentation.
- `clients/`: Android and Windows client source and build instructions.
- The separate **Platypus-complete-submission.zip** contains the Android APK,
  Windows applications, full fictional demo database, and START-DEMO.ps1.
  Download the complete bundle from the project submission's download link.

## Run from source
Use Python 3.11 or newer. From `backend/`:
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:APP_ENV = "development"
$env:MAIL_DISABLED = "1"
$env:LOGIN_OTP_REQUIRED = "0"
.\.venv\Scripts\python.exe seed.py --demo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Run these commands in a fresh checkout: `seed.py --demo` resets its target database.
Patient: http://127.0.0.1:8000/ ; hospital: http://127.0.0.1:8000/staff/
The seed command prints the newly created demo account credentials.
For the existing presentation dataset, use START-DEMO.ps1 in the complete bundle instead.
See `backend/DEPLOY.md` for production hosting and `clients/README.md` for native builds.
Native clients require a running backend. Temporary Cloudflare links can change.

## Demo data notice
All included identities and medical data are fictional, as confirmed by the project owner.
They do not represent real patients. Open-source provenance has not been independently
verified for every sample; externally sourced materials require their original attribution.
Secrets and current session/database data are excluded from this Git repository.
The separate database download is for demonstrations only.
