"""Checks on text before it leaves the machine or lands in a report.

The deny-terms file has the improvement harness's format (`privacy.deny_terms_file`): one
term per line, `#` starts a comment, matching ignores case. The file stays outside the repo.
"""

import re
from pathlib import Path

from .errors import BoundaryError, InputError

# Shapes follow verification-loop's secret gate.
_SECRET_SHAPES = {
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "aws_access_key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "anthropic_key": r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}",
    "openai_key": r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "github_pat": r"github_pat_[A-Za-z0-9_]{22,}",
    "google_key": r"AIza[0-9A-Za-z_-]{35}",
    "slack_token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "url_credentials": r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:(?P<password>[^\s/@]+)@",
}
_SECRETS = {name: re.compile(pattern) for name, pattern in _SECRET_SHAPES.items()}

# A URL password that is only a template slot is not a credential: `${DB_PASSWORD}`, `$TOKEN`,
# `<PAT>`, `{{ token }}`, a printf verb such as `%s`, or a run of `*`. IaC and docs write
# connection strings this way, and refusing them would stop every run over such a repository.
_PLACEHOLDER = re.compile(
    r"\$\{[^{}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|<[^<>]+>|\{\{[^{}]*\}\}|%[A-Za-z]|\*+"
)


def _has_secret(name, pattern, text):
    if name != "url_credentials":
        return pattern.search(text) is not None
    return any(not _PLACEHOLDER.fullmatch(match["password"]) for match in pattern.finditer(text))


def find_secrets(text):
    """Names of the secret shapes present in text, in a stable order."""
    return [name for name, pattern in _SECRETS.items() if _has_secret(name, pattern, text)]


def load_deny_terms(path):
    if not path:
        return []
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise InputError(f"cannot read the deny-terms file: {error.strerror}") from None
    terms = (line.strip() for line in lines)
    return [term.lower() for term in terms if term and not term.startswith("#")]


def find_deny_terms(text, terms):
    lowered = text.lower()
    return [term for term in terms if term in lowered]


def assert_sendable(text, terms, what):
    """Stop before text with a denied term leaves the machine. The term itself is not echoed."""
    hits = find_deny_terms(text, terms)
    if hits:
        raise BoundaryError(
            f"refused to send {what}: it contains {len(hits)} term(s) from the deny-terms file."
        )
