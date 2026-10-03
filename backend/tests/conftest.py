"""Suite-wide test environment (W6). Runs before any test module imports `app`.

* Tests must NEVER send real email, even if a developer's local .env contains SMTP credentials.
* Older tests (and the demo seed helpers) register / log in with password only. Those two switches
  keep that behaviour for them; the OTP tests (tests/test_otp_*.py) turn the switches ON per test.
  Production/.env.example default both to 1.
"""
import os
import tempfile

# Never touch data/app.db: if no support module picked a temp DB yet, pick one now (before any `app` import).
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_test_"), "test.db"))
# Tests call job functions directly; the background scheduler would race them.
os.environ["HP_DISABLE_SCHEDULER"] = "1"

# PROD: the suite runs in development mode (console OTP fallback, no forced Secure cookies, no auth
# rate limit); tests/test_prod_mode.py flips APP_ENV=production per test.
os.environ["APP_ENV"] = "development"
os.environ["COOKIE_SECURE"] = "0"
os.environ["AUTH_RATE_LIMIT_PER_MINUTE"] = "0"
# Real SMTP credentials live in .env; set every SMTP_* key (even empty) so load_dotenv(override=False)
# can never fill them in.
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["SMTP_FROM"] = ""
os.environ["DEV_SHOW_OTP"] = os.environ.get("DEV_SHOW_OTP", "0")
os.environ.setdefault("LOGIN_OTP_REQUIRED", "0")
os.environ.setdefault("SIGNUP_VERIFY_REQUIRED", "0")
