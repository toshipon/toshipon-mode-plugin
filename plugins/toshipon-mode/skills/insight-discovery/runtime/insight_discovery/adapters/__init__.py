"""Input adapters. Each turns one source into raw items; evidence.py normalizes them."""

from . import claude_insights, ledger, manual, review

ADAPTERS = {
    "claude_insights": claude_insights.collect,
    "manual": manual.collect,
    "ledger": ledger.collect,
    "review": review.collect,
}
