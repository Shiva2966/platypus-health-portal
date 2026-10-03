"""Send exactly ONE test email using the SMTP settings in .env / the environment.

    .venv\\Scripts\\python.exe scripts\\send_test_email.py you@example.com

Prints only the outcome and the masked recipient - never credentials. Exit code 0 = sent.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings  # noqa: E402,F401  (loads .env)
from app.services import mailer  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", argv[0]):
        print("Usage: send_test_email.py <recipient email>")
        return 2
    to = argv[0]
    if not mailer.smtp_configured():
        print("NOT SENT: SMTP isn't configured (SMTP_USER and SMTP_PASSWORD must both be set in .env).")
        return 1
    try:
        mailer.send_test_email(to)
    except mailer.MailError as e:  # messages are written to be secret-free
        print(f"FAILED to send to {mailer.mask_email(to)}: {e}")
        return 1
    except Exception as e:  # never echo exception text: it could contain server responses
        print(f"FAILED to send to {mailer.mask_email(to)}: unexpected {type(e).__name__}")
        return 1
    print(f"SENT one test email to {mailer.mask_email(to)} via {mailer._env('SMTP_HOST', 'smtp.gmail.com')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
