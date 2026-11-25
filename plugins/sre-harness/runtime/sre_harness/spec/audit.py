"""`spec audit`: an independent audit of the analysis along eight axes, in its own backend call.

1. Input: the findings and PRs of `findings.json`, the registry, the trace, the document
   sections and, when `spec report` already ran, the patches. All of it is DATA in the prompt.
2. The auditor is `agents/spec-auditor.md`: the same file is the Claude Code subagent and,
   below a short task, the instructions of the call. The answer (`llm-spec-audit`) carries
   findings, limitations and the axes it could not assess; it has no field for a status.
3. Checks, in this order, on every answer:
   - the schema (`llm.Session.ask`);
   - finding ids are unique;
   - each piece of evidence names an id of the input, and a quote after it is in that item's
     text (`check_finding`). A failing medium or low finding is dropped with its reasons; a
     failing critical or high finding refuses the whole answer, so a severe claim is either
     checked or the audit fails, never silently lost;
   - limitations do not park a severe concern (`check_limitations`): no severity word, no
     input id that no kept finding cites, no finding id, no claim repeated.
   A refused or schema-breaking answer is asked again, with the problems named in the
   instructions, up to `max_iterations` calls in all. The analysis itself is not run again:
   apart from inferred links and explanation text it is fixed rules over git and the
   registry, and the audit's findings do not feed it.
4. Status by rule (`axis_status`, `axes`): critical or high -> fail, only medium or low ->
   concerns, no finding -> pass when the axis was assessed, else not_assessed. An axis is not
   assessed when the audit did not complete, the auditor said so, every finding of the axis
   was dropped, or the input is empty. Any kept critical or high finding blocks: exit 1, and
   audit.json is written all the same.
"""

import re
from pathlib import Path

from .. import llm, paths, records
from ..errors import BoundaryError, InputError, QualityGateError

STAGE = "audit"
OUTPUT_SCHEMA = "llm-spec-audit"
AGENT = Path(__file__).resolve().parents[3] / "agents" / "spec-auditor.md"
AXES = (
    "coverage",
    "correctness",
    "consistency",
    "traceability",
    "verifiability",
    "clarity",
    "feasibility",
    "operability",
)
SEVERITIES = ("critical", "high", "medium", "low")
BLOCKING = ("critical", "high")
DEFAULT_ITERATIONS = 3
MAX_ITERATIONS = 10
FILE = "audit.json"

_ID = r"(?:DOC-[0-9]{4,}(?:#S[0-9]{1,6})?|(?:FD|DEC|REQ|TL|PT)-[0-9]{4,})"
_EVIDENCE = re.compile(rf"({_ID})(?::\s*(.+))?", re.DOTALL)
_NAMED_ID = re.compile(rf"(?<![A-Za-z0-9-]){_ID}(?![0-9])")
_FINDING_ID = re.compile(r"(?<![A-Za-z0-9-])F[0-9]+(?![A-Za-z0-9])")
_SEVERITY_WORDS = re.compile(
    r"(?i)\b(?:critical|high|severe|severity|blockers?|blocking|fatal)\b"
    r"|重大|致命|深刻|クリティカル"
)
_QUOTES = (('"', '"'), ("「", "」"), ("“", "”"), ("'", "'"))
MIN_REPEAT = 12

TASK = """\
Task: audit the specification analysis in DATA independently, along the eight axes named in
`axes`. `findings` are what the analysis reported, `prs` the open pull requests it read,
`decisions` and `requirements` the registry, `trace` the links, `documents` the source
sections, `patches` the candidate fixes when there are any. Return `findings` (ids F1, F2, ...,
each with one category from the axes), `limitations` and `not_assessed`. Every evidence entry is
an id from DATA (FD-0001, DEC-0001, REQ-0001, TL-0001, PT-0001, DOC-0001 or DOC-0001#S1),
optionally followed by ": " and a quote copied exactly from that item. Do not give a status,
a score or a verdict: the harness computes them from the severities.

The instructions of the spec-auditor agent follow."""

RETRY = """\
Your previous answer was refused for these reasons (codes and ids only):
{problems}
Answer again, keeping every severe concern in `findings` with checkable evidence."""


def instructions(problems=()):
    text = AGENT.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]
    value = f"{TASK}\n\n{text.strip()}"
    if problems:
        listed = "\n".join(f"- {problem}" for problem in problems[:20])
        value += "\n\n" + RETRY.format(problems=listed)
    return value


# Status by rule ---------------------------------------------------------------------------


def axis_status(severities, assessed, rejected=False):
    """The one rule for an axis: its kept findings first, then whether it was assessed."""
    if any(severity in BLOCKING for severity in severities):
        return "fail"
    if severities:
        return "concerns"
    if not assessed or rejected:
        return "not_assessed"
    return "pass"


def axes(findings, not_assessed, rejected_axes, completed):
    out = {}
    for axis in AXES:
        mine = [item for item in findings if item["category"] == axis]
        assessed = completed and axis not in not_assessed
        out[axis] = {
            "status": axis_status(
                [item["severity"] for item in mine], assessed, axis in rejected_axes
            ),
            "finding_ids": [item["id"] for item in mine],
        }
    return out


def blocking(findings):
    return [item["id"] for item in findings if item["severity"] in BLOCKING]


# Input and the validator ------------------------------------------------------------------


def _text(*parts):
    return " ".join(" ".join(str(part).split()) for part in parts if part)


def build_index(data):
    """{id: text} of everything the audit may cite. A quote must be a substring of the text."""
    index = {}
    for item in data.get("findings", []):
        notes = [entry.get("note", "") for entry in item.get("evidence", [])]
        refs = [entry.get("ref", "") for entry in item.get("evidence", [])]
        index[item["id"]] = _text(
            item.get("explanation"), item.get("key"), *item.get("entities", []), *notes, *refs
        )
    for item in data.get("decisions", []):
        index[item["id"]] = _text(
            item.get("external_id"), item.get("title"), item.get("content"), item.get("rationale")
        )
    for item in data.get("requirements", []):
        criteria = [
            _text(entry.get("given"), entry.get("when"), entry.get("then"))
            for entry in item.get("acceptance_criteria", [])
        ]
        spans = [entry.get("span", "") for entry in item.get("ambiguity", [])]
        index[item["id"]] = _text(
            item.get("title"), item.get("statement"), item.get("rationale"), *criteria, *spans
        )
    for item in data.get("trace", []):
        evidence = item.get("evidence", {})
        index[item["id"]] = _text(
            item["from"]["id"], item["to"]["id"], evidence.get("ref"), evidence.get("quote")
        )
    for item in data.get("patches", []):
        index[item["id"]] = _text(item.get("finding_id"), *item.get("paths", []))
    for document in data.get("documents", []):
        sections = []
        for section in document.get("sections", []):
            text = _text(section.get("heading"), section.get("text"))
            index[f"{document['id']}#{section['id']}"] = text
            sections.append(text)
        index[document["id"]] = _text(document.get("title"), *sections)
    return index


def _unquoted(text):
    text = " ".join(text.split())
    for left, right in _QUOTES:
        if len(text) > 2 and text.startswith(left) and text.endswith(right):
            return text[len(left) : -len(right)].strip()
    return text


def check_finding(finding, index):
    """Reasons the finding cannot be kept, empty when every piece of evidence checks out."""
    reasons = []
    for position, entry in enumerate(finding["evidence"]):
        match = _EVIDENCE.fullmatch(entry.strip())
        if not match:
            reasons.append(f"evidence_format: evidence[{position}]")
            continue
        name, quote = match.group(1), match.group(2)
        if name not in index:
            reasons.append(f"unknown_id: evidence[{position}] {name}")
        elif quote is not None and _unquoted(quote) not in index[name]:
            reasons.append(f"quote_not_found: evidence[{position}] {name}")
    return reasons


def _normal(text):
    return " ".join(text.lower().split()).rstrip(" .。")


def check_limitations(limitations, findings):
    """Problems of limitations that would hide a severe or specific concern outside findings."""
    cited = {
        match.group(1)
        for item in findings
        for entry in item["evidence"]
        for match in [_EVIDENCE.match(entry.strip())]
        if match
    }
    claims = [_normal(item["claim"]) for item in findings]
    problems = []
    for position, text in enumerate(limitations):
        where = f"limitations[{position}]"
        if _SEVERITY_WORDS.search(text):
            problems.append(f"{where} severity_word: severity belongs on a finding")
        if _FINDING_ID.search(text):
            problems.append(f"{where} names_finding: limitations do not qualify findings")
        for name in sorted(set(_NAMED_ID.findall(text)) - cited):
            problems.append(f"{where} names_id: {name} is named but no finding cites it")
        plain = _normal(text)
        for number, claim in enumerate(claims):
            short, long = sorted((claim, plain), key=len)
            if len(short) >= MIN_REPEAT and short in long:
                problems.append(f"{where} repeats_claim: {findings[number]['id']}")
    return problems


def _read(run_dir):
    found_path = paths.child(run_dir, "findings.json")
    if not found_path.is_file():
        raise InputError("the run has no findings yet: run `spec analyze` first.")
    found = records.read_json(found_path)
    records.validate("impact-report", found, "findings_file")
    links = records.read_json(paths.child(run_dir, "trace.json"))
    records.validate("trace-link", links, "file")
    decisions = records.read_json(paths.child(run_dir, "decisions.json"))
    requirements = records.read_json(paths.child(run_dir, "requirements.json"))
    documents = records.read_jsonl(paths.child(run_dir, "documents.jsonl"))
    return found, links, decisions, requirements, documents


def _patches(run_dir, finding_ids):
    directory = paths.child(run_dir, "patches")
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("PT-*.json")):
        item = records.read_json(path)
        if item.get("finding_id") in finding_ids:
            out.append({"id": item["id"], "finding_id": item["finding_id"], "paths": item["paths"]})
    return out


def audit_input(run_dir):
    """The DATA of the call. `patches` are left out of the fingerprint: `spec report` writes
    them after the audit, and that must not make the audit stale."""
    found, links, decisions, requirements, documents = _read(run_dir)
    keep = ("key", "missing")
    data = {
        "axes": list(AXES),
        "findings": [
            {
                **{
                    name: item[name]
                    for name in ("id", "kind", "severity", "basis", "entities", "prs")
                },
                "explanation": item["explanation"],
                "evidence": item["evidence"],
                **{name: item[name] for name in keep if name in item},
            }
            for item in found["findings"]
        ],
        "analysis_limitations": found["limitations"],
        "prs": [
            {
                "number": pr["number"],
                "title": pr.get("title", ""),
                "stale": pr["stale"],
                **({"missing": pr["missing"]} if pr.get("missing") else {}),
            }
            for pr in found["prs"]
        ],
        "decisions": [
            {
                name: item[name]
                for name in (
                    "id",
                    "external_id",
                    "title",
                    "content",
                    "rationale",
                    "status",
                    "supersedes",
                    "superseded_by",
                    "basis",
                )
                if name in item
            }
            for item in decisions["decisions"]
        ],
        "requirements": [
            {
                name: item[name]
                for name in (
                    "id",
                    "title",
                    "statement",
                    "acceptance_criteria",
                    "rationale",
                    "nfr_category",
                    "decision_refs",
                    "ambiguity",
                    "basis",
                )
                if name in item
            }
            for item in requirements["requirements"]
        ],
        "trace": [
            {name: item[name] for name in ("id", "from", "to", "relation", "basis", "evidence")}
            for item in links["links"]
        ],
        "documents": [
            {
                "id": document["id"],
                "kind": document["kind"],
                "title": document["title"],
                "locator": document["provenance"].get("locator", ""),
                "sections": [
                    {"id": section["id"], "heading": section["heading"], "text": section["text"]}
                    for section in document["sections"]
                ],
            }
            for document in documents
        ],
    }
    fingerprint = records.fingerprint(data)
    data["patches"] = _patches(run_dir, {item["id"] for item in found["findings"]})
    return data, fingerprint


def fingerprint(run_dir):
    return audit_input(run_dir)[1]


# The stub ----------------------------------------------------------------------------------

STUB_LIMITATION = (
    "The stub applies fixed rules to the registry and the analysis; it does not read the "
    "documents and says nothing about how well a model would audit."
)


def stub_audit(data):
    """Fixed rules over the registry and the analysis, never over document text, so nothing a
    document says can change them. Feasibility and operability have no rule."""
    findings = []

    def add(severity, category, evidence, claim, step):
        findings.append(
            {
                "id": f"F{len(findings) + 1}",
                "severity": severity,
                "category": category,
                "claim": claim,
                "evidence": evidence,
                "next_experiment": step,
            }
        )

    by_kind = {}
    for item in data["findings"]:
        by_kind.setdefault(item["kind"], []).append(item)
    for item in by_kind.get("contradiction", []):
        if not item["prs"]:
            add(
                "high",
                "consistency",
                [item["id"], *item["entities"][:1]],
                f"The default branch and the registry disagree on {item.get('key', 'a value')}.",
                "Decide in the source document which value holds, then ingest and analyze again.",
            )
    for item in data["findings"]:
        if item["kind"] in ("stale_sha", "input_missing") and item["prs"]:
            add(
                "medium",
                "coverage",
                [item["id"]],
                f"PR #{item['prs'][0]} was not analyzed, so its impact is unknown.",
                "Run `spec analyze` again once the head can be read.",
            )
        if item["basis"] == "inferred":
            add(
                "low",
                "correctness",
                [item["id"]],
                "This finding rests on inferred trace links only.",
                "Confirm the link in the source and write the id there.",
            )
    linked = {end for link in data["trace"] for end in (link["from"]["id"], link["to"]["id"])}
    for item in data["decisions"]:
        if item.get("status") == "approved" and item["id"] not in linked:
            add(
                "medium",
                "traceability",
                [item["id"]],
                "An approved decision has no trace link to a requirement, design or test.",
                "Name the decision id in the requirement or design that follows it.",
            )
    for item in data["requirements"]:
        criteria = item.get("acceptance_criteria", [])
        if not any(entry.get("measurable") for entry in criteria):
            add(
                "medium",
                "verifiability",
                [f"{item['id']}: {item['statement']}"],
                "The requirement has no measurable acceptance criterion.",
                "Add a given / when / then criterion with a metric, a target and a unit.",
            )
        if item["id"] not in linked:
            add(
                "medium",
                "traceability",
                [item["id"]],
                "No trace link reaches the requirement.",
                "Name the requirement id in the design, IaC or test that implements it.",
            )
        vague = [entry for entry in item.get("ambiguity", []) if entry["kind"] == "vague_term"]
        if vague:
            add(
                "low",
                "clarity",
                [item["id"]],
                "The requirement uses a vague term.",
                "Replace the term with a measurable statement.",
            )
    return {
        "findings": findings,
        "limitations": [STUB_LIMITATION],
        "not_assessed": [
            {"axis": axis, "reason": "the stub has no rule for this axis"}
            for axis in ("feasibility", "operability")
        ],
    }


# The stage ---------------------------------------------------------------------------------


def _last_call(run_dir, before):
    rows = llm.calls(run_dir)
    return rows[-1]["id"] if len(rows) > before else None


def _judge(output, index):
    """(kept, rejected, problems). Problems refuse the whole answer."""
    problems, kept, rejected, seen = [], [], [], set()
    for item in output["findings"]:
        if item["id"] in seen:
            problems.append(f"duplicate_id: {item['id']}")
        seen.add(item["id"])
    for item in output["findings"]:
        reasons = check_finding(item, index)
        if not reasons:
            kept.append(item)
            continue
        rejected.append(
            {
                "id": item["id"],
                "category": item["category"],
                "severity": item["severity"],
                "reasons": reasons,
            }
        )
        if item["severity"] in BLOCKING:
            problems.append(
                f"unchecked_{item['severity']}: {item['id']} ({'; '.join(reasons)[:200]})"
            )
    problems += check_limitations(output["limitations"], kept)
    return kept, rejected, problems


def audit(run_dir, session, max_iterations=DEFAULT_ITERATIONS):
    """(audit.json value, exit code). The value is written to the run in every case but a
    refusal at the boundary (a deny term or a secret shape: nothing was sent)."""
    if isinstance(max_iterations, bool) or not 1 <= max_iterations <= MAX_ITERATIONS:
        raise InputError(f"--max-iterations must be 1-{MAX_ITERATIONS}.")
    data, digest = audit_input(run_dir)
    index = build_index(data)
    attempts, problems, accepted = [], [], None
    for attempt in range(1, max_iterations + 1):
        before = len(llm.calls(run_dir))
        try:
            output = session.ask(STAGE, instructions(problems), data, OUTPUT_SCHEMA, stub_audit)
        except BoundaryError:
            raise
        except QualityGateError as error:
            # The record keeps the message; the next prompt gets only a code, so no text of
            # the refused answer travels back into the instructions.
            problems = ["error: no schema-valid answer (the schema or the call failed)"]
            attempts.append(
                {
                    "attempt": attempt,
                    "call_id": _last_call(run_dir, before),
                    "outcome": "error",
                    "problems": [str(error)[:300]],
                }
            )
            continue
        kept, rejected, problems = _judge(output, index)
        attempts.append(
            {
                "attempt": attempt,
                "call_id": session.last_call_id,
                "outcome": "refused" if problems else "accepted",
                "problems": [problem[:300] for problem in problems],
            }
        )
        if not problems:
            accepted = (output, kept, rejected)
            break
    has_input = bool(data["findings"] or data["requirements"] or data["decisions"])
    if accepted is None:
        output, kept, rejected = {"limitations": [], "not_assessed": []}, [], []
        status = "failed"
    else:
        output, kept, rejected = accepted
        status = "blocked" if blocking(kept) else "passed"
    named = {entry["axis"] for entry in output["not_assessed"]}
    value = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": run_dir.name,
        "created_at": records.iso(),
        "backend": session.backend,
        "status": status,
        "input_sha256": digest,
        "max_iterations": max_iterations,
        "attempts": attempts,
        "findings": kept,
        "limitations": list(output["limitations"]),
        "not_assessed": list(output["not_assessed"]),
        "rejected_findings": rejected,
        "axes": axes(
            kept,
            named,
            {item["category"] for item in rejected},
            completed=accepted is not None and has_input,
        ),
        "blocking": blocking(kept),
    }
    records.validate("spec-audit", value)
    records.write_json(paths.child(run_dir, FILE), value)
    return value, (0 if status == "passed" else 1)


def summary(value):
    return {
        "run_id": value["run_id"],
        "status": value["status"],
        "axes": {axis: entry["status"] for axis, entry in value["axes"].items()},
        "blocking": value["blocking"],
        "findings": len(value["findings"]),
        "rejected_findings": len(value["rejected_findings"]),
        "attempts": len(value["attempts"]),
        "files": [FILE],
    }


# For `spec report` ------------------------------------------------------------------------


def unassessed():
    return axes([], set(), set(), completed=False)


def load(run_dir):
    """(report view, audit.json value or None). Without a current audit.json every axis is
    not_assessed: a missing or stale audit never reads as a pass."""
    path = paths.child(run_dir, FILE)
    if not path.is_file():
        return {"ref": None, "status": "not_run", "axes": unassessed(), "blocking": []}, None
    value = records.read_json(path)
    records.validate("spec-audit", value)
    if value["input_sha256"] != fingerprint(run_dir):
        return {"ref": FILE, "status": "stale", "axes": unassessed(), "blocking": []}, None
    view = {
        "ref": FILE,
        "status": value["status"],
        "axes": value["axes"],
        "blocking": value["blocking"],
    }
    return view, value
