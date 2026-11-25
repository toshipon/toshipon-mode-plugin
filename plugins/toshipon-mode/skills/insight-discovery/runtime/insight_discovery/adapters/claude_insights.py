"""Claude Code `/insights` leftovers under usage-data/: facets/ and session-meta/.

The format is not published. This file is the only one that knows it, it reads structured
fields only, and anything in another shape is counted as unrecognized instead of guessed at.
Free-text fields (summaries, goals, first prompts, project paths) are never looked at, and
neither is anything outside usage-data/.
"""

from collections import Counter
from pathlib import Path

from ..foundation import sha256
from .base import Unrecognized, identifier, listed, load_object, safe_path

FACET_KEYS = ("friction_counts", "goal_categories", "outcome")
META_KEYS = ("tool_error_categories", "user_interruptions", "tool_errors", "duration_minutes")


def default_path():
    return Path.home() / ".claude" / "usage-data"


def _sessions(directory, expected, limits, stats):
    """session id -> object, for the files in one directory that look like the known shape."""
    sessions = {}
    if not directory.is_dir() or directory.is_symlink():
        return sessions
    for path in listed(directory, ["*.json"], limits, stats):
        try:
            data = load_object(path, limits)
            if not any(key in data for key in expected):
                raise Unrecognized("none of the known fields")
        except Unrecognized:
            stats["format_unrecognized"] += 1
            continue
        session = data.get("session_id")
        sessions[session if isinstance(session, str) and session else path.stem] = data
    return sessions


def _counted(data, field, stats):
    """(name, count) pairs of a {name: count} field. A different shape is reported, not coerced."""
    if field not in data:
        stats[f"missing.{field}"] += 1
        return []
    value = data[field]
    if not isinstance(value, dict):
        stats["format_unrecognized"] += 1
        return []
    pairs = []
    for name, count in sorted(value.items()):
        if identifier(name) and isinstance(count, int) and not isinstance(count, bool):
            if count > 0:
                pairs.append((name, count))
        else:
            stats["dropped_field"] += 1
    return pairs


def collect(path, context):
    stats, limits = Counter(), context["limits"]
    base = safe_path(path) if path else default_path()
    if not base.is_dir() or base.is_symlink():
        stats["source_missing"] += 1
        return [], stats
    facets = _sessions(base / "facets", FACET_KEYS, limits, stats)
    metas = _sessions(base / "session-meta", META_KEYS, limits, stats)
    if not facets and not metas and not stats["format_unrecognized"]:
        # Only a report.html, or a layout this adapter has never seen.
        stats["format_unrecognized"] += 1
    raws = []
    for session in sorted(set(facets) | set(metas)):
        facet, meta = facets.get(session, {}), metas.get(session, {})
        unit = sha256(session)[:16]
        goals = _counted(facet, "goal_categories", stats) if facet else []
        missing = [] if goals else ["goal_categories"]
        goal = max(goals, key=lambda pair: (pair[1], pair[0]))[0] if goals else "unknown"
        common = {
            "task_key": f"goal:{goal}"[:100],
            "domain_tag": "claude-code-usage",
            "unit": unit,
            "classification": "internal",
            "missing": missing,
        }
        found = [
            ("friction", f"friction:{name}", count, f"facets/{unit}")
            for name, count in (_counted(facet, "friction_counts", stats) if facet else [])
        ] + [
            ("failure", f"tool_error:{name}", count, f"session-meta/{unit}")
            for name, count in (_counted(meta, "tool_error_categories", stats) if meta else [])
        ]
        interruptions = meta.get("user_interruptions")
        if isinstance(interruptions, int) and not isinstance(interruptions, bool):
            if interruptions > 0:
                found.append(
                    (
                        "manual_intervention",
                        "user_interruption",
                        interruptions,
                        f"session-meta/{unit}",
                    )
                )
        elif meta:
            stats["missing.user_interruptions"] += 1
        for kind, label, count, locator in found:
            raws.append(
                {
                    **common,
                    "kind": kind,
                    "label": label[:100],
                    "occurrences": count,
                    "locator": locator,
                }
            )
    return raws, stats
