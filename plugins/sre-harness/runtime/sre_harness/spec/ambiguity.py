"""Rule-based ambiguity checks on a requirement. The vocabulary is data, Japanese and English only.

Kinds (all `detector: rule`):

- `vague_term`: a term from `VAGUE_TERMS` in the statement or a criterion's `then`.
- `unbounded`: a term from `UNBOUNDED_TERMS` (always, never, 無期限 ...).
- `no_criteria`: no acceptance criterion.
- `no_metric`: a quality requirement (`nfr_category` set) with no number and unit anywhere.
- `compound`: one sentence of the statement carries two or more obligations (must ... and
  must ..., しなければならず ... しなければならない).

English terms match whole words, ignoring case; Japanese terms match as substrings. No term
in a table is a substring of another, so one place is reported once. Text in other languages
gets no rule-based check; only a model's (`detector: llm`) findings, if any.
"""

import re

VAGUE_TERMS = (
    ("適切に", "ja"),
    ("適切な", "ja"),
    ("できるだけ", "ja"),
    ("なるべく", "ja"),
    ("可能な限り", "ja"),
    ("高速", "ja"),
    ("迅速", "ja"),
    ("速やかに", "ja"),
    ("十分な", "ja"),
    ("柔軟に", "ja"),
    ("必要に応じて", "ja"),
    ("簡単に", "ja"),
    ("効率的", "ja"),
    ("as appropriate", "en"),
    ("appropriately", "en"),
    ("as soon as possible", "en"),
    ("as much as possible", "en"),
    ("fast", "en"),
    ("quickly", "en"),
    ("user-friendly", "en"),
    ("efficiently", "en"),
    ("sufficient", "en"),
    ("flexible", "en"),
    ("robust", "en"),
    ("if necessary", "en"),
    ("as needed", "en"),
    ("easy", "en"),
    ("reasonable", "en"),
    ("etc.", "en"),
)
UNBOUNDED_TERMS = (
    ("常に", "ja"),
    ("無制限", "ja"),
    ("無期限", "ja"),
    ("永久に", "ja"),
    ("いつでも", "ja"),
    ("always", "en"),
    ("never", "en"),
    ("unlimited", "en"),
    ("forever", "en"),
    ("indefinitely", "en"),
    ("at all times", "en"),
)
# Obligation markers for `compound`.
OBLIGATIONS = (
    (r"\b(?:must|shall|should)\b", "en"),
    (r"しなければなら|ねばなら|すること|する必要", "ja"),
)
UNITS = (
    "milliseconds", "ms", "seconds", "second", "sec", "s", "minutes", "minute", "min",
    "hours", "hour", "h", "days", "day", "weeks", "week", "months", "month", "years", "year",
    "%", "KiB", "MiB", "GiB", "TiB", "KB", "MB", "GB", "TB", "rps", "qps", "req/s",
    "ミリ秒", "秒", "分", "時間", "日", "週間", "か月", "ヶ月", "年", "件", "回",
)  # fmt: skip
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])([0-9]+(?:\.[0-9]+)?)\s*("
    + "|".join(re.escape(unit) for unit in sorted(UNITS, key=len, reverse=True))
    + r")(?![A-Za-z])"
)
_SENTENCE = re.compile(r"[^.!?。]+[.!?。]?")


# Japanese terms that also occur inside an unrelated word: 常に in 非常に ("very").
_JA_NOT_AFTER = {"常に": "非"}


def _pattern(term, language):
    if language == "ja":
        before = _JA_NOT_AFTER.get(term)
        return re.compile((f"(?<!{before})" if before else "") + re.escape(term))
    return re.compile(rf"(?<![A-Za-z0-9-]){re.escape(term)}(?![A-Za-z0-9-])", re.IGNORECASE)


_VAGUE = [_pattern(term, language) for term, language in VAGUE_TERMS]
_UNBOUNDED = [_pattern(term, language) for term, language in UNBOUNDED_TERMS]
_OBLIGATIONS = [re.compile(pattern, re.IGNORECASE) for pattern, _ in OBLIGATIONS]


def measure(text):
    """(target, unit) of the first number with a unit in text, or None."""
    match = _NUMBER.search(text or "")
    return (float(match.group(1)), match.group(2)) if match else None


def fields(requirement):
    """(field name, text) pairs a span can come from."""
    yield "statement", requirement["statement"]
    for index, criterion in enumerate(requirement["acceptance_criteria"]):
        for part in ("given", "when", "then"):
            if criterion.get(part):
                yield f"acceptance_criteria[{index}].{part}", criterion[part]


def _terms(requirement, kind, patterns):
    """Terms in the statement and the expected outcomes (`then`). `given` and `when` describe
    the situation under test, where "never answers" is a precondition, not a vague promise."""
    found = []
    for field, text in fields(requirement):
        if field.endswith((".given", ".when")):
            continue
        hits = sorted(
            (match.start(), match.group(0))
            for pattern in patterns
            for match in pattern.finditer(text)
        )
        found += [{"kind": kind, "span": span, "field": field} for _, span in hits]
    return found


def _compound(statement):
    found = []
    for sentence in _SENTENCE.findall(statement):
        count = sum(len(pattern.findall(sentence)) for pattern in _OBLIGATIONS)
        if count >= 2 and sentence.strip():
            found.append({"kind": "compound", "span": sentence.strip(), "field": "statement"})
    return found


def detect(requirement):
    """Rule findings for a requirement (`statement`, `acceptance_criteria`, `nfr_category`)."""
    found = _terms(requirement, "vague_term", _VAGUE)
    found += _terms(requirement, "unbounded", _UNBOUNDED)
    found += _compound(requirement["statement"])
    if not requirement["acceptance_criteria"]:
        found.append({"kind": "no_criteria"})
    if requirement.get("nfr_category") and not any(
        measure(text) for _, text in fields(requirement)
    ):
        found.append({"kind": "no_metric"})
    return [{**entry, "detector": "rule"} for entry in found]
