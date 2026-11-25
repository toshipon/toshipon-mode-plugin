"""Normalize what adapters return into evidence, drop duplicates, and append it to the ledger."""

import re
from collections import Counter

from . import state as st
from .adapters import ADAPTERS
from .adapters.base import identifier
from .foundation import (
    SCHEMAS,
    Refusal,
    agents,
    canonical,
    find_secrets,
    iso,
    load_deny_terms,
    redact_text,
    sha256,
    validate,
)

KINDS = (
    "friction",
    "retry",
    "failure",
    "manual_intervention",
    "constraint",
    "review_finding",
    "hypothesis_outcome",
    "unexpected_success",
    "user_note",
)
MASK = "[REDACTED]"
ANOMALIES = ("format_unrecognized", "source_missing", "corrupt_ledger", "oversize")


def deny_terms(config):
    """Forbidden terms from the foundation's privacy.deny_terms_file. A configured file must load."""
    path = config["privacy"].get("deny_terms_file")
    try:
        return load_deny_terms(path)
    except OSError:
        raise Refusal(f"privacy.deny_terms_file is set but cannot be read: {path}") from None


def scrub(text, terms):
    """(text with secrets and forbidden terms masked, what was masked)."""
    flags = []
    if find_secrets(text):
        text, flags = redact_text(text), ["redacted_secret"]
    for term in terms:
        masked = re.sub(re.escape(term), MASK, text, flags=re.IGNORECASE)
        if masked != text:
            text, flags = masked, [*flags, "masked_deny_term"]
    return text, flags


def _clean(value, terms):
    """The identifier, or None when it is not one or names something that must not be kept."""
    name = identifier(value)
    if name and (find_secrets(name) or any(term in name.lower() for term in terms)):
        return None
    return name


def build(raw, adapter, terms, limits, now):
    """(evidence, flags) or (None, [reason]). Nothing is cut short or filled in to make it fit."""
    kind, flags = raw.get("kind"), []
    if kind not in KINDS:
        return None, ["excluded_unknown_kind"]
    classification = raw.get("classification", "internal")
    if classification not in agents.CLASSIFICATIONS:
        return None, ["excluded_bad_classification"]
    # Identifiers are stored and shown too: one that carries a forbidden term or a secret
    # is replaced, exactly like one that is not a plain identifier.
    task, label, domain = (
        _clean(raw.get(key), terms) for key in ("task_key", "label", "domain_tag")
    )
    if any(raw.get(key) and not value for key, value in (("label", label), ("domain_tag", domain))):
        flags.append("replaced_identifier")
    if not task:
        task = f"task:{sha256(str(raw.get('task_key')))[:12]}"
        flags.append("hashed_task_key")
    text = raw.get("text")
    if text is not None:
        text = str(text).strip()
        if not text:
            return None, ["excluded_empty_text"]
        if len(text) > limits["max_text_chars"]:
            return None, ["excluded_text_too_long"]
        text, masked = scrub(text, terms)
        flags += masked
    unit = raw.get("unit")
    identity = canonical([kind, task, label or kind, text or "", unit or ""])
    evidence = {
        "schema_version": 1,
        "id": f"EV-{sha256(identity)[:12]}",
        "source": {
            "adapter": adapter,
            "locator": raw.get("locator") or adapter,
            "content_sha256": sha256(identity),
            "retrieved_at": iso(now),
        },
        "kind": kind,
        "label": label or kind,
        "context": {"task_key": task, "domain_tag": domain or task},
        "counts": {
            "occurrences": raw.get("occurrences", 1),
            "sessions": raw.get("sessions", 1),
        },
        "classification": classification,
        "missing": list(raw.get("missing", [])),
    }
    if unit:
        evidence["context"]["unit"] = unit
    if text:
        evidence["text"] = text
    return validate(f"{SCHEMAS}:evidence", evidence), flags


def collect(home, adapter, path, config, settings, now, known=()):
    """(new evidence, stats) from one source. Reads only; nothing is written."""
    if adapter not in ADAPTERS:
        raise Refusal(f"unknown adapter: {adapter}")
    raws, stats = ADAPTERS[adapter](path, {"home": home, "limits": settings["limits"]})
    terms, seen, added = deny_terms(config), set(known), []
    stats = Counter(stats)
    stats["read"] = len(raws)
    for raw in raws:
        item, flags = build(raw, adapter, terms, settings["limits"], now)
        stats.update(flags)
        if not item:
            continue
        if item["id"] in seen:
            stats["duplicate"] += 1
            continue
        seen.add(item["id"])
        added.append(item)
    stats["added"] = len(added)
    return added, {key: count for key, count in sorted(stats.items()) if count}


def ingest(home, adapter, path, config, settings, now):
    """Append the new evidence of one source. Returns the stats, whether or not anything was new."""
    state = st.load(home)
    added, stats = collect(home, adapter, path, config, settings, now, known=state["evidence"])
    anomalies = {key: stats.get(key, 0) for key in ANOMALIES}
    previous = state["ingests"].get(adapter, {})
    changed = any(anomalies[key] != previous.get(key, 0) for key in ANOMALIES)
    if added or changed:
        st.append(home, "ingested", {"adapter": adapter, "evidence": added, "stats": stats}, now)
    return stats
