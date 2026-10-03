"""Password hashing (argon2id) + strength policy for W6 flows.

- New passwords are hashed with argon2id. Existing/seed accounts hashed with W1's scrypt scheme
  (app.security) still verify, so demo accounts keep working.
- Policy: >= 10 chars, not a known common/breached password (bundled list - offline, no network),
  not built from the email/name. Optional online check: set HIBP_CHECK=1 to also ask the
  Have I Been Pwned range API (k-anonymity: only the first 5 chars of the SHA-1 leave the server).
"""
import hashlib
import logging
import os
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

log = logging.getLogger("health-portal.auth")

MIN_LENGTH = 10
MAX_LENGTH = 128

_ph = PasswordHasher()  # argon2id, library defaults (t=3, m=64 MiB, p=4)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """Verify argon2id hashes, falling back to W1's scrypt scheme for seed/legacy accounts."""
    if not stored:
        return False
    if stored.startswith("$argon2"):
        try:
            return _ph.verify(stored, password)
        except (VerificationError, InvalidHashError):
            return False
        except Exception:  # pragma: no cover - defensive
            return False
    from app import security  # legacy scrypt$... hashes

    return security.verify_password(password, stored)


_DUMMY = _ph.hash("not-a-real-password-for-timing")


def burn_password_time(password: str) -> None:
    """Spend the same time as a real check when the account doesn't exist (no timing oracle)."""
    try:
        _ph.verify(_DUMMY, password)
    except Exception:
        pass


# A compact offline list of very common / breached passwords (all compared lower-case).
_COMMON = set("""
password password1 password12 password123 password1234 password12345 passw0rd p@ssw0rd p@ssword pa55word pa55w0rd
123456 1234567 12345678 123456789 1234567890 12345678910 0123456789 1111111111 0000000000 1234512345 123123123
qwerty qwerty123 qwertyuiop qwerty1234 qwertyuiop123 qwerty12345 asdfghjkl asdfghjkl1 zxcvbnm zxcvbnm123 1q2w3e4r
1q2w3e4r5t 1qaz2wsx 1qaz2wsx3edc qazwsxedc q1w2e3r4t5 abc123 abc12345 abcd1234 abcdefg123 abcdefghij abcd123456
iloveyou iloveyou1 iloveyou123 iloveu123 letmein letmein123 welcome welcome1 welcome123 welcome1234 admin admin123
admin1234 admin12345 administrator root toor changeme changeme123 default guest guest123 login login123 master
monkey monkey123 dragon dragon123 football football1 baseball baseball1 soccer hockey batman superman sunshine
sunshine1 princess princess1 shadow shadow123 trustno1 whatever starwars freedom michael jordan jordan23 hunter2
harley ranger buster thomas tigger charlie jennifer jessica ashley daniel hannah summer winter spring autumn
secret secret123 mypassword mypassword1 mypassword123 myspace1 internet computer computer1 samsung samsung1
google google123 facebook facebook1 youtube hello123 hello1234 helloworld hello12345 test test123 test1234 testing
testing123 testtest temp temp123 user user123 pass pass123 pass1234 passpass qweasd qweasdzxc 654321 666666 888888
121212 112233 159753 147258369 987654321 9876543210 11111111 22222222 33333333 55555555 77777777 99999999
00000000 aaaaaaaa aaaaaaaaaa 1234qwer 1234abcd asdf1234 zaq12wsx q1w2e3r4 qwe123456 qwer1234 qwer12345 qwerasdf
health healthy health123 hospital hospital1 doctor doctor123 nurse nurse123 patient patient1 patient123 medical
medical123 medicine clinic clinic123 hospital123 password! password# password@ letmein! welcome! welcome@123
admin@123 admin@1234 india123 india@123 india1234 krishna123 ganesh123 sai12345 jaishreeram cricket cricket123
iloveyou2 iloveyou! lovelove lovely123 loveyou123 baby123 babygirl babygirl1 football123 mustang mustang1 master123
""".split())

_LEET = str.maketrans("@$!01345", "asioleas")  # best-effort "un-leet": @->a $->s !->i 0->o 1->l 3->e 4->a 5->s


def _squash(p: str) -> str:
    return re.sub(r"[^a-z0-9]", "", p.lower())


def _hibp_pwned(password: str) -> bool:  # optional, off by default
    import urllib.request

    sha = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 - HIBP protocol requires SHA-1
    req = urllib.request.Request(f"https://api.pwnedpasswords.com/range/{sha[:5]}",
                                 headers={"User-Agent": "health-portal-demo", "Add-Padding": "true"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310
            for line in r.read().decode().splitlines():
                h, _, n = line.partition(":")
                if h == sha[5:] and int(n.strip() or 0) > 0:
                    return True
    except Exception:  # network trouble must never block sign-up
        log.warning("HIBP check unavailable; skipped")
    return False


def validate_password(password: str, *, email: str = "", name: str = "") -> str | None:
    """Return a plain-language problem, or None if the password is acceptable."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    low = password.lower()
    squashed = _squash(password)
    if low in _COMMON or squashed in _COMMON or low.translate(_LEET) in _COMMON:
        return "That password is too common and has appeared in data breaches. Please choose a different one."
    stripped = squashed.rstrip("0123456789")
    if stripped in _COMMON or (stripped and stripped in {"password", "qwerty", "welcome", "iloveyou", "letmein", "admin"}):
        return "That password is too easy to guess. Try a few unrelated words, or a longer phrase."
    if len(set(password)) <= 3:
        return "That password repeats the same few characters. Please choose something less predictable."
    if re.fullmatch(r"\d+", password):
        return "Use more than just numbers."
    local = (email or "").split("@")[0].lower()
    if len(local) >= 4 and local in squashed:
        return "Your password shouldn't contain your email name."
    for part in re.split(r"\s+", (name or "").lower()):
        if len(part) >= 4 and part in squashed:
            return "Your password shouldn't contain your name."
    seq = "abcdefghijklmnopqrstuvwxyz0123456789"
    if squashed in seq or squashed in seq[::-1] or squashed in "qwertyuiopasdfghjklzxcvbnm":
        return "That password is a simple keyboard or alphabet sequence. Please choose a different one."
    if os.environ.get("HIBP_CHECK") == "1" and _hibp_pwned(password):
        return "That password has appeared in a known data breach. Please choose a different one."
    return None
