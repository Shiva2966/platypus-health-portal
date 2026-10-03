"""Integration-pass regressions (QA-07, QA-08, QA-09, QA-10 in BUGS.md)."""
import logging
import re
from pathlib import Path

from app.routers import auth_otp
from app.services import mailer

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_no_demo_credentials_in_static_files_or_readme():
    """QA-07: the staff sign-in page (or any shipped asset) never shows the shared demo password."""
    for f in list(STATIC.rglob("*.js")) + list(STATIC.rglob("*.html")) + [ROOT / "README.md"]:
        text = f.read_text(encoding="utf-8", errors="ignore")
        for secret in ("Staff-Demo-2026", "Demo-Patient-2026"):
            assert secret not in text, f"{secret} found in {f}"
    assert "Demo accounts" not in _read("app/static/staff/staff_login.js")


def test_sign_in_screen_is_removed_after_login():
    """QA-09: after sign-in the OTP screen is emptied (not just hidden) and focus moves to the page."""
    js = _read("app/static/js/shell.js")
    boot = js[js.index("async function boot()"):]
    boot = boot[:boot.index("Portal.boot = boot")]
    assert 'Portal.clear(document.getElementById("auth-view"))' in boot
    assert 'aria-hidden' in boot and ".focus()" in boot
    assert 'S.clear(document.getElementById("login-view")).hidden = true' in _read("app/static/staff/staff_app.js")


def test_field_helpers_link_labels_to_inputs():
    """QA-10: upload dialog (and other docs/sharing forms) labels use for/id."""
    for rel in ("app/static/js/docs_documents.js", "app/static/js/docs_sharing.js"):
        js = _read(rel)
        fn = js[js.index("function field("):]
        fn = fn[:fn.index("\n  }\n")]
        assert "input.id" in fn and "for: input.id" in fn, rel


def test_every_text_label_has_for_or_wraps_its_input():
    """Sweep: `el("label", { text: ... })` without `for` leaves the control unnamed."""
    bad = []
    for f in STATIC.rglob("*.js"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r'el\("label", \{([^}]*)\}', line):
                attrs = m.group(1)
                if "text:" in attrs and "for:" not in attrs:
                    bad.append(f"{f.name}:{n}")
    assert bad == []


def test_code_screens_show_didnt_get_it_hint():
    """QA-08: anti-enumeration "code sent" stays, but the user gets a resend / check-spam hint."""
    js = _read("app/static/js/auth_flow.js")
    assert "function notArrivedHint" in js and js.count("notArrivedHint()") >= 2
    assert "spam" in js


def test_background_send_failure_is_logged_without_secrets(monkeypatch, caplog):
    def boom(*a, **k):
        raise mailer.MailError("Couldn't reach the email server (OSError).")

    monkeypatch.setattr(mailer, "send_otp_email", boom)
    with caplog.at_level(logging.ERROR):
        auth_otp._send_bg("someone@gmail.com", "654321", "signup")
    text = caplog.text
    assert "OTP email failed" in text and "s***@gmail.com" in text
    assert "654321" not in text and "someone@gmail.com" not in text
