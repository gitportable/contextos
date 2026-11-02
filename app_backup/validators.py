"""Input validation shared by the API and unit tests."""
import re

_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_COMMON = {"password", "password1", "1234567890", "qwertyuiop", "letmein123", "iloveyou12", "contextos123", "admin12345"}


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    """Practical RFC 5322 subset: one @, dot-atom local part, DNS-style domain with a real TLD, sane lengths."""
    e = (email or "").strip()
    if not e or len(e) > 254 or e.count("@") != 1 or any(c.isspace() for c in e):
        return False
    local, domain = e.split("@")
    if not local or len(local) > 64 or not _LOCAL.match(local):
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL.match(l) for l in labels):
        return False
    tld = labels[-1]
    return len(tld) >= 2 and tld.isalpha()


def password_problems(password: str, email: str = "") -> list[str]:
    out = []
    if len(password) < 10:
        out.append("at least 10 characters")
    if len(password) > 200:
        out.append("at most 200 characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        out.append("a mix of letters and numbers")
    if password.lower() in _COMMON:
        out.append("not a commonly used password")
    local = normalize_email(email).split("@")[0]
    if local and len(local) >= 4 and local in password.lower():
        out.append("not contain your email name")
    return out
