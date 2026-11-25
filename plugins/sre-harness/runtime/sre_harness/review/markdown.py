"""External text in Markdown the harness writes (evaluation.md, the approval screen).

Inline text is escaped so that links, images, HTML, headings and tables cannot form, and
autolinks are broken. Quotes go into a fenced code block longer than any backtick run inside.
Control characters (terminal escape sequences among them) are replaced before anything else.
"""

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~:-])")
_FENCE = re.compile(r"`{3,}")


def clean(text):
    return _CONTROL.sub("�", str(text))


def escape(text, limit=400):
    """One line of inline text. Newlines become spaces; long text is cut with a visible mark."""
    text = " ".join(clean(text).split())
    if len(text) > limit:
        text = text[:limit] + " …(cut)"
    return _SPECIAL.sub(r"\\\1", text)


def code_block(text, info="text"):
    text = clean(text)
    longest = max((len(run) for run in _FENCE.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"
