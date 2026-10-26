"""Best-effort secret redaction applied to everything baton writes."""
import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
]

_BARE_END = r"(?![\w.(\[)])"  # a bare value followed by . ( [ ) is code, not a literal
_PREFIX = r"\b[A-Za-z0-9_\-]*"  # so `gf_security_admin_password=` counts, `max_tokens=` does not

# api keys / tokens: quoted strings, or bare values that look like keys (digit/uppercase, 8+ chars),
# so code like `api_key=settings.key` or `token=access_token)` is left alone.
_KEYISH = re.compile(
    r"(?i)(" + _PREFIX + r"(?:aws_secret_access_key|api[_-]?key|token))(\s*[=:]\s*)"
    r"(?:(['\"])[^'\"\s]{6,}\3|(?=[A-Za-z0-9+/_\-]*[0-9A-Z])[A-Za-z0-9+/_\-]{8,}" + _BARE_END + ")"
)

# passwords: any bare or quoted value of 4+ chars, except obvious placeholders. Short and lowercase
# values like `admin` are real passwords, so this errs on the side of redacting.
_PASSWORDISH = re.compile(
    r"(?i)(" + _PREFIX + r"(?:password|passwd|pwd|secret))(\s*[=:]\s*)"
    r"(?:(['\"])[^'\"\s]{4,}\3|[A-Za-z0-9+/_\-]{4,}" + _BARE_END + ")"
)
_PLACEHOLDERS = {"none", "null", "true", "false", "string", "str", "required", "password", "secret", "example"}


def _keep_placeholder(m: re.Match) -> str:
    value = m.group(0)[len(m.group(1)) + len(m.group(2)):].strip("'\"").lower()
    return m.group(0) if value in _PLACEHOLDERS else f"{m.group(1)}{m.group(2)}[REDACTED]"


def redact(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub("[REDACTED]", text)
    text = _KEYISH.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return _PASSWORDISH.sub(_keep_placeholder, text)
