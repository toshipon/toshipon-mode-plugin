"""Secret detection. Logs are redacted on write; publish candidates are blocked on a hit."""

import re
from pathlib import Path

REDACTED = "[REDACTED]"

# Whole-match shapes. Token shapes follow verification-loop's secret gate, plus the ones
# that turn up in tool errors: private key blocks, bearer/basic headers, URLs with credentials.
_TOKENS = {
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*)",
    "aws_access_key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "anthropic_key": r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}",
    "openai_key": r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    "stripe_key": r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "github_pat": r"github_pat_[A-Za-z0-9_]{22,}",
    "google_key": r"AIza[0-9A-Za-z_-]{35}",
    "slack_token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "slack_webhook": r"hooks\.slack\.com/services/[A-Za-z0-9/]{20,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "authorization": r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{16,}=*",
}
_PATTERNS = {name: re.compile(pattern, re.DOTALL) for name, pattern in _TOKENS.items()}
# scheme://user:password@host keeps everything but the password.
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)")
# key = value, where the key ENDS in a credential word. `max_tokens`, `token_budget` and
# `passwordless` are ordinary settings and do not match.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9])
    ((?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|password|passwd|pass|secret|token|credentials?))
    (["']?\s*[:=]\s*)
    (?:"([^"\n]{4,})"|'([^'\n]{4,})'|([^\s"',;]{4,}))
    """
)
_HARMLESS_VALUES = re.compile(
    r"(?i)^(?:true|false|null|none|yes|no|enabled|disabled|\[redacted\])$"
)


def _assigned_secret(match):
    value = match.group(3) or match.group(4) or match.group(5)
    return None if _HARMLESS_VALUES.match(value) else value


def find_secrets(text):
    """Names of the secret shapes present in text, in a stable order."""
    hits = [name for name, pattern in _PATTERNS.items() if pattern.search(text)]
    if _URL.search(text):
        hits.append("url_credentials")
    if any(_assigned_secret(match) for match in _ASSIGNMENT.finditer(text)):
        hits.append("credential_assignment")
    return hits


def _redact_assignment(match):
    value = _assigned_secret(match)
    if value is None:
        return match.group(0)
    quote = '"' if match.group(3) else "'" if match.group(4) else ""
    return f"{match.group(1)}{match.group(2)}{quote}{REDACTED}{quote}"


def redact_text(text):
    for pattern in _PATTERNS.values():
        text = pattern.sub(REDACTED, text)
    text = _URL.sub(lambda match: f"{match.group(1)}{REDACTED}{match.group(3)}", text)
    return _ASSIGNMENT.sub(_redact_assignment, text)


def redact(value):
    """Copy of a JSON value with every secret-shaped substring replaced."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def load_deny_terms(path):
    """One forbidden term per line (client names, internal hosts). The file stays outside the repo."""
    if not path:
        return []
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def find_deny_terms(text, terms):
    lowered = text.lower()
    return [term for term in terms if term in lowered]
