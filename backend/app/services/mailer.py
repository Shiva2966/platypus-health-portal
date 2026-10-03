"""Outgoing email (OTP codes + security notices) over SMTP with STARTTLS.

Configuration comes ONLY from environment variables / a git-ignored `.env` file:
    SMTP_HOST (default smtp.gmail.com)   SMTP_PORT (default 587)
    SMTP_USER                            SMTP_PASSWORD   <- Gmail *App Password*, never put it in code
    SMTP_FROM (default SMTP_USER)        APP_NAME (default "My Health Records")
    SMTP_TIMEOUT (seconds, default 15)

DEV MODE: if SMTP_USER / SMTP_PASSWORD are not set, nothing is sent. The code is printed to the
server console instead (so demos and tests work); the API only returns it when DEV_SHOW_OTP=1.
PRODUCTION (APP_ENV=production, the default): no console fallback - sending raises MailNotConfigured.

SAFETY: OTP codes and passwords are never written through the logging module. The only place a
code is ever printed is the DEV-mode console line, which is disabled as soon as SMTP is configured.
Emails contain no health information - only the app name, the code and generic advice.
"""
import html
import logging
import os
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

try:  # .env support (python-dotenv); real environment variables always win
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=False)
except Exception:  # pragma: no cover - dotenv missing is not fatal
    pass

log = logging.getLogger("health-portal.mailer")


class MailError(Exception):
    """Sending failed. The message is safe to log / show (never contains secrets or codes)."""


@dataclass
class SendResult:
    mode: str  # "smtp" (really sent) | "dev" (printed to console) | "skipped" (reserved test domain)
    delivered: bool


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def app_name() -> str:
    return _env("APP_NAME", "My Health Records")


def smtp_configured() -> bool:
    """False when MAIL_DISABLED=1 (kill switch for dev/test processes that inherit a real .env)."""
    if _env("MAIL_DISABLED") in ("1", "true", "yes"):
        return False
    return bool(_env("SMTP_USER") and _env("SMTP_PASSWORD") and _env("SMTP_HOST", "smtp.gmail.com"))


def mask_email(addr: str) -> str:
    """j***@gmail.com - used in logs and UI hints."""
    local, _, domain = (addr or "").partition("@")
    if not domain:
        return "***"
    return (local[:1] + "***") + "@" + domain


# ---------------- templates ----------------

_PURPOSE_TEXT = {
    "signup": ("Confirm your email", "Use this code to confirm your email address and finish creating your account."),
    "login": ("Your sign-in code", "Use this code to finish signing in."),
    "reset": ("Reset your password", "Use this code to choose a new password."),
    "email_change": ("Confirm your new email", "Use this code to confirm your new email address."),
}


def _otp_message(to: str, code: str, purpose: str, ttl_minutes: int) -> EmailMessage:
    name = app_name()
    heading, intro = _PURPOSE_TEXT.get(purpose, ("Your verification code", "Use this code to continue."))
    subject = f"Your {name} verification code"
    spaced = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
    text = (
        f"{name}\n\n{heading}\n\n{intro}\n\n    Your code: {code}\n\n"
        f"This code expires in {ttl_minutes} minutes and can be used once.\n\n"
        "If you didn't ask for this, you can ignore this email - nobody can get into your account without it.\n"
        f"For your safety, {name} will never ask you for this code by phone, text or chat.\n"
    )
    e = html.escape
    page = f"""<!doctype html><html lang="en"><body style="margin:0;padding:0;background:#f3f6fa;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f6fa;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border-radius:12px;border:1px solid #d9e1ea;font-family:Segoe UI,Arial,sans-serif;color:#17212b;">
<tr><td style="background:#0b4f8a;color:#ffffff;padding:16px 24px;border-radius:12px 12px 0 0;font-size:18px;font-weight:600;">{e(name)}</td></tr>
<tr><td style="padding:24px;">
<h1 style="font-size:20px;margin:0 0 12px 0;">{e(heading)}</h1>
<p style="font-size:16px;line-height:1.5;margin:0 0 20px 0;">{e(intro)}</p>
<p style="text-align:center;margin:0 0 20px 0;"><span style="display:inline-block;font-size:34px;letter-spacing:8px;font-weight:700;font-family:Consolas,Menlo,monospace;background:#eef4fb;border:1px solid #b9cde3;border-radius:10px;padding:12px 20px;">{e(spaced)}</span></p>
<p style="font-size:15px;line-height:1.5;margin:0 0 12px 0;">This code expires in <strong>{ttl_minutes} minutes</strong> and works only once.</p>
<p style="font-size:15px;line-height:1.5;margin:0 0 12px 0;">If you didn&rsquo;t ask for this, you can safely ignore this email &mdash; nobody can get into your account without the code.</p>
<p style="font-size:13px;line-height:1.5;color:#52606d;margin:16px 0 0 0;">{e(name)} will never ask you for this code by phone, text or chat.</p>
</td></tr></table>{_demo_footer()}
</td></tr></table></body></html>"""
    return _build(to, subject, text, page)


def _notice_message(to: str, subject: str, headline: str, lines: list[str]) -> EmailMessage:
    name = app_name()
    text = f"{name}\n\n{headline}\n\n" + "\n\n".join(lines) + "\n\nIf this wasn't you, reset your password right away.\n"
    e = html.escape
    paras = "".join(f'<p style="font-size:16px;line-height:1.5;margin:0 0 12px 0;">{e(x)}</p>' for x in lines)
    page = f"""<!doctype html><html lang="en"><body style="margin:0;padding:24px 12px;background:#f3f6fa;font-family:Segoe UI,Arial,sans-serif;color:#17212b;">
<div style="max-width:480px;margin:0 auto;background:#fff;border:1px solid #d9e1ea;border-radius:12px;padding:24px;">
<div style="font-weight:600;color:#0b4f8a;margin-bottom:12px;">{e(name)}</div>
<h1 style="font-size:20px;margin:0 0 12px 0;">{e(headline)}</h1>{paras}
<p style="font-size:14px;color:#52606d;">If this wasn&rsquo;t you, reset your password right away.</p></div></body></html>"""
    return _build(to, subject, text, page)


def _demo_footer() -> str:
    try:
        from app.settings import show_demo_banner

        shown = show_demo_banner()
    except Exception:
        shown = False
    if not shown:
        return ""
    return ('\n<p style="font-family:Segoe UI,Arial,sans-serif;font-size:12px;color:#7b8794;margin:12px 0 0 0;">'
            "Demo application &mdash; synthetic data only.</p>")


def _build(to: str, subject: str, text: str, page: str) -> EmailMessage:
    sender = _env("SMTP_FROM") or _env("SMTP_USER") or "no-reply@localhost"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((app_name(), sender)) if "<" not in sender else sender
    msg["To"] = to
    msg["Reply-To"] = _env("SMTP_REPLY_TO") or sender
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1].strip("<> ") or "localhost"))
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    return msg


# ---------------- transport ----------------

def _deliver(msg: EmailMessage) -> None:
    host = _env("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(_env("SMTP_PORT", "587"))
    except ValueError:
        raise MailError("SMTP_PORT must be a number (587 for Gmail).") from None
    try:
        timeout = float(_env("SMTP_TIMEOUT", "15"))
    except ValueError:
        timeout = 15.0
    user, password = _env("SMTP_USER"), _env("SMTP_PASSWORD")
    last = ""
    for attempt in (1, 2):  # one retry on transient problems
        try:
            if port == 465:
                client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=timeout)
            with client as s:
                s.ehlo()
                if port != 465:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                s.login(user, password)
                s.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            # Retrying would only risk locking the account; tell the operator what to fix.
            raise MailError("The email server rejected the login. For Gmail, use a 16-character App Password "
                            "(not your normal password) in SMTP_PASSWORD - see docs/EMAIL_SETUP.md.") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailError("The email server refused the recipient address.") from None
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            last = type(exc).__name__  # class name only: messages could echo credentials/addresses
            log.warning("SMTP attempt %d/2 failed (%s)", attempt, last)
            if attempt == 1:
                time.sleep(float(_env("SMTP_RETRY_DELAY", "1")))
    raise MailError(f"Couldn't reach the email server ({last}). Check SMTP_HOST/SMTP_PORT and your internet connection.")


class MailNotConfigured(MailError):
    """Production without SMTP credentials: refuse instead of falling back to the console."""


def _production() -> bool:
    from app.settings import is_production

    return is_production()


_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_TLDS = ("test", "invalid", "example", "localhost")


def is_reserved_recipient(addr: str) -> bool:
    """RFC 2606/6761 reserved domains (seed/demo/test accounts). Real SMTP never sends there: bounces to
    these addresses damage the sender's reputation."""
    domain = (addr or "").rpartition("@")[2].strip().strip(".").lower()
    if not domain:
        return True
    if domain in _RESERVED_DOMAINS or any(domain.endswith("." + d) for d in _RESERVED_DOMAINS):
        return True
    return domain.rpartition(".")[2] in _RESERVED_TLDS


def send_otp_email(to: str, code: str, purpose: str, ttl_minutes: int = 10) -> SendResult:
    """Send (or, in development without SMTP, print) a one-time code. Raises MailError if sending fails;
    MailNotConfigured in production without SMTP (codes are never printed there)."""
    if not smtp_configured():
        if _production():
            raise MailNotConfigured("Email sending isn't configured (SMTP_USER / SMTP_PASSWORD).")
        # DEV MODE: console only. This is the one deliberate place a code is printed.
        print(f"[DEV MODE - email not configured] {purpose} code for {to}: {code}  (valid {ttl_minutes} min)", flush=True)
        return SendResult("dev", False)
    if is_reserved_recipient(to):
        log.warning("Skipped OTP email to reserved test domain to=%s", mask_email(to))
        return SendResult("skipped", False)
    _deliver(_otp_message(to, code, purpose, ttl_minutes))
    log.info("OTP email sent purpose=%s to=%s", purpose, mask_email(to))  # never the code
    return SendResult("smtp", True)


def send_notice_email(to: str, subject: str, headline: str, lines: list[str]) -> SendResult:
    """Security notice (password changed, 'someone tried to sign up with your email'). No codes, no PHI."""
    if not smtp_configured():
        if _production():
            raise MailNotConfigured("Email sending isn't configured (SMTP_USER / SMTP_PASSWORD).")
        print(f"[DEV MODE - email not configured] notice for {to}: {headline}", flush=True)
        return SendResult("dev", False)
    if is_reserved_recipient(to):
        log.warning("Skipped notice email to reserved test domain to=%s", mask_email(to))
        return SendResult("skipped", False)
    _deliver(_notice_message(to, subject, headline, lines))
    log.info("Notice email sent to=%s", mask_email(to))
    return SendResult("smtp", True)


def send_test_email(to: str) -> SendResult:
    """One real test message (no code, no PHI). Raises MailError / MailNotConfigured."""
    if not smtp_configured():
        raise MailNotConfigured("Email sending isn't configured (SMTP_USER / SMTP_PASSWORD).")
    if is_reserved_recipient(to):
        raise MailError("That is a reserved test domain (example.*, .test, .invalid); use a real mailbox.")
    name = app_name()
    _deliver(_notice_message(to, f"{name} test email", "Email delivery works",
                             [f"This is a test message from {name}. Verification codes will arrive the same way.",
                              "No action is needed."]))
    log.info("Test email sent to=%s", mask_email(to))
    return SendResult("smtp", True)
