"""Checks on text before it leaves the machine or lands in a report.

The deny-terms file has the improvement harness's format (`privacy.deny_terms_file`): one
term per line, `#` starts a comment, matching ignores case. The file stays outside the repo.
"""

import re
import unicodedata
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


# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties): drawn as nothing, so a word can
# carry them unseen. Hangul fillers, variation selectors and tags are among them.
_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_DROPPED = frozenset({"Mn", "Me", "Cc", "Cf"})  # combining marks, control and format
_KEPT = frozenset("\n\t")  # they separate words; dropping them would join two lines


def _strip(text):
    text = _IGNORABLE.sub("", text)
    return "".join(c for c in text if c in _KEPT or unicodedata.category(c) not in _DROPPED)


def examined(text):
    """The form text is checked in, never printed: NFKC (a full-width letter is the letter),
    without default-ignorable characters, combining marks (Mn, Me), control and format
    characters (Cc, Cf), newlines and tabs aside. Stripped before and after NFKC, since NFKC
    can turn one ignorable character into another."""
    return _strip(unicodedata.normalize("NFKC", _strip(text)))


def forms(text):
    """The text as read and in its examined form, for a check that must see through either."""
    return text + "\n" + examined(text)


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
