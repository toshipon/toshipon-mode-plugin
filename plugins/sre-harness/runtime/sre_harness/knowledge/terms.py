"""Splitting text into search terms, and estimating tokens. Both rules match the project
context explorer's (words and Japanese bigrams; 3 UTF-8 bytes per token) so the two indexes
count alike. They are copied, not imported: the plugins install separately."""

import math
import re

ESTIMATOR = "utf8_bytes_div_3"
_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿]+")


def terms(text):
    """Lowercase words of two characters or more, and character bigrams for Japanese, which
    has no spaces to split on. Sorted and unique, so the same text gives the same terms."""
    text = text.lower()
    found = {word for word in _WORD.findall(text) if len(word) > 1}
    for run in _CJK.findall(text):
        found |= {run[i : i + 2] for i in range(len(run) - 1)}
    return sorted(found)


def estimate_tokens(text):
    """A deliberately high estimate: 3 UTF-8 bytes per token, rounded up."""
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return math.ceil(len(data) / 3)
