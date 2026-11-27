"""The `knowledge` block of a review-registry version: where an approved rule sits in the
knowledge index (scope, access, enforcement, applies_when, exceptions), as the person who
approved it gave it to `review approve`.

Only a person makes a rule mandatory, and only here: the block is shown on the approval screen
and the hash they type covers it (`confirmation`). Every other source of the index is advisory.

    build       the block from the approve options, checked (exit 2), or None when no option
                was given: then the version is written exactly as before the block existed
    placement   the block that places an entry: its latest version's when that is approved,
                otherwise the latest approved version's (a retirement keeps the rule where it
                was, for the readers it had). None: the project defaults of the adapter
    describe    the lines of the approval screen that show the block

The block needs knowledge/config.json: its project, component and readers are checked against
it, and what is not given is taken from it (the project that holds the run's repo, at project
level, and that project's default access).
"""

from dataclasses import dataclass

from .. import records
from ..errors import InputError
from ..review.markdown import code_block, escape
from . import config, model, untrusted

DEFINITION = "knowledge"  # review-registry $defs/knowledge
LEVELS = ("organization", "project", "component")  # what --knowledge-scope offers
ENFORCEMENTS = ("advisory", "mandatory")


@dataclass(frozen=True)
class Options:
    """What the person gave on the command line; empty fields were not given."""

    scope: str | None = None
    project: str | None = None
    component: str | None = None
    classification: str | None = None
    readers: tuple = ()
    enforcement: str | None = None
    applies_when: tuple = ()
    exceptions: tuple = ()

    def given(self):
        return any(
            (
                self.scope,
                self.project,
                self.component,
                self.classification,
                self.readers,
                self.enforcement,
                self.applies_when,
                self.exceptions,
            )
        )


def _scope(settings, repo, options):
    if options.scope is None:
        if options.project or options.component:
            raise InputError(
                "--knowledge-project and --knowledge-component need --knowledge-scope."
            )
        project_id, found = config.project_of(settings, repo)
        if project_id is None:
            raise InputError(
                f"the run's repo {repo} gives no project in knowledge/config.json ({found}); "
                "give --knowledge-scope."
            )
        return {"level": "project", "project_id": project_id, "component_id": None, "task_id": None}
    scope = {
        "level": options.scope,
        "project_id": options.project,
        "component_id": options.component,
        "task_id": None,
    }
    # The scope invariant is the index's own: ids below the level null, ids down to it given.
    broken = model.problems(
        {"status": "approved", "approval": True, "enforcement": "advisory", "scope": scope}
    )
    if broken:
        raise InputError("the knowledge scope is inconsistent: " + "; ".join(broken) + ".")
    if options.project is not None:
        project = settings["projects"].get(options.project)
        if project is None:
            raise InputError(f"project {options.project!r} is not in knowledge/config.json.")
        if options.component is not None and options.component not in project["components"]:
            raise InputError(
                f"component {options.component!r} is not a component of {options.project} in "
                "knowledge/config.json."
            )
    return scope


def _access(settings, repo, scope, options):
    project_id = scope["project_id"] or config.project_of(settings, repo)[0]
    default = settings["projects"][project_id]["default_access"] if project_id else {}
    classification = options.classification or default.get("classification")
    readers = sorted(set(options.readers or default.get("readers", ())))
    if classification is None or not readers:
        raise InputError(
            "no project gives this rule a default access; give --knowledge-classification and "
            "--knowledge-reader."
        )
    unknown = sorted(set(readers) - config.known_readers(settings))
    if unknown:
        raise InputError(
            "readers not held by any principal of knowledge/config.json: " + ", ".join(unknown)
        )
    return {"classification": classification, "readers": readers}


def _sentences(name, values):
    for value in values:
        if value != value.strip() or any(untrusted.invisible(c) for c in value):
            raise InputError(
                f"{name} must be one line without control characters or surrounding spaces."
            )
    return list(values)


def build(settings, repo, options):
    """The checked block for a rule of `repo`, or None when no option was given."""
    if options is None or not options.given():
        return None
    if settings is None:
        raise InputError(
            "a knowledge block needs knowledge/config.json under the state root: its projects "
            "and principals decide which scope and readers the rule may have."
        )
    scope = _scope(settings, repo, options)
    block = {
        "scope": scope,
        "access": _access(settings, repo, scope, options),
        "enforcement": options.enforcement or "advisory",
        "applies_when": _sentences("--knowledge-applies-when", options.applies_when),
        "exceptions": _sentences("--knowledge-exception", options.exceptions),
    }
    problems = records.violations("review-registry", block, DEFINITION)
    if problems:
        raise InputError("the knowledge block is invalid: " + "; ".join(problems[:5]))
    return block


def confirmation(content_sha256, block):
    """The hash the person types the start of: the artifact's alone without a block, the
    artifact's and the block's together with one, so changing the block changes it."""
    if block is None:
        return content_sha256
    return records.fingerprint({"content_sha256": content_sha256, DEFINITION: block})


def placement(versions):
    """The block that places an entry (see the module doc), or None."""
    latest = versions[-1]
    if latest["status"] == "approved":
        return latest.get(DEFINITION)
    approved = [version for version in versions if version["status"] == "approved"]
    return approved[-1].get(DEFINITION) if approved else None


def _scope_text(scope):
    names = [scope[name] for name in model.SCOPE_IDS if scope[name] is not None]
    return " ".join([scope["level"], *names])


def describe(block, content_sha256):
    """Markdown lines of the approval screen for the block. The person's sentences are escaped."""
    access = block["access"]
    lines = [
        "## Knowledge index placement (recorded; the typed hash covers it)",
        "",
        f"- confirmation_sha256: `{confirmation(content_sha256, block)}` "
        "(content_sha256 and this block together)",
        # Names and levels passed the schema (letters, digits, '.', '_', '-'): shown as they are.
        f"- scope: {_scope_text(block['scope'])}",
        f"- access: {access['classification']}; readers " + ", ".join(access["readers"]),
    ]
    if block["enforcement"] == "mandatory":
        lines.append(
            "- enforcement: **MANDATORY**. Every `knowledge search` in this scope returns this "
            "rule whatever the query, before any advisory record; a search whose budget cannot "
            "hold its mandatory rules stops (mandatory_overflow)."
        )
    else:
        lines.append("- enforcement: advisory")
    for name in ("applies_when", "exceptions"):
        shown = "; ".join(escape(text) for text in block[name]) or "none"
        lines.append(f"- {name}: {shown}")
    lines += ["", code_block(records.canonical(block), "json"), ""]
    return lines
