"""`spec analyze`: the registry, the default branch and every open PR to `findings.json`.

Order of work:

1. Read the run's registries (`spec extract` must have run) and the open PR list.
2. Fetch the default branch and the PR heads into the harness's bare repository (`prs`).
   A PR whose head did not arrive is `input_missing`. A PR whose fetched head differs from
   the head the list gave is `stale` (its head moved in between): it gets one `stale_sha`
   finding and is left out of everything else, and the stage exits 1.
3. Read the default branch's IaC, test and Markdown files (`structure`) and build the trace
   (`trace`). With `--backend`, the backend may propose inferred links for documents that name
   no ID; each proposal is kept only if its quote is in the document and its target exists.
4. Default-branch findings: `contradiction` (a fact that differs from the registry's, or two
   files that disagree), `not_reflected` (an active decision or an explicit requirement that no
   file names, or a registry fact no file states), `superseded_dependency`, `input_missing`
   (references to ids the registry does not have, and the registry's own gaps) and
   `ambiguous_requirement`.
5. Per PR, against its merge base with the default branch: `impact` (registry entities the
   changed units reach along the trace), `superseded_dependency` and `input_missing` for
   references the PR adds, `missing_test` (a requirement whose facts the PR changes with no
   changed test that names it), and the PR's fact changes for the conflict index.
6. `semantic_conflict`: per (entity, key), the PRs that set it to different values, minus the
   pairs whose diffs overlap on the same base (git reports those as merge conflicts; they are
   listed in `limitations`). Candidates come from the index only; the backend, when given,
   writes the explanation text and nothing else.

Nothing here decides approval or state. Severity is fixed per kind (`findings`).
"""

from .. import paths, records
from ..errors import InputError, QualityGateError
from . import findings as fd
from . import prs as git_prs
from . import structure, trace

STAGE = "analyze"
OUTPUT_SCHEMA = "llm-spec-analyze"
TITLE_MINIMUM = 7
RULES_VERSION = 1
FILE_NODES = ("iac", "test")

LINK_INSTRUCTIONS = """\
Task: `documents` in DATA are files of the repository's default branch that name no decision or
requirement id. `entities` are the decisions and requirements of the registry. For each
document that clearly refines, depends on, implements or verifies an entity, give
{path, to, relation, quote}: path as given, to an entity id, relation one of refines,
depends_on, implements, verifies, conflicts_with, and quote copied exactly from the document.
Links whose quote is not in the document, or whose target is not an entity, are dropped.
Do not judge approval, priority or correctness. Return an empty `explanations`."""

EXPLAIN_INSTRUCTIONS = """\
Task: each item of `conflicts` in DATA is a pair or group of open pull requests that set the
same structured key of the same entity to different values. Write one short explanation per
conflict (key as given): what each PR sets and why both cannot hold. Do not decide which PR is
right and do not suggest merging or closing. Return an empty `links`."""


# Inputs -----------------------------------------------------------------------------------


def _registry(run_dir):
    names = ("decisions.json", "requirements.json")
    if not all(paths.child(run_dir, name).is_file() for name in names):
        raise InputError("the run has no registry yet: run `spec extract` first.")
    decisions = records.read_json(paths.child(run_dir, "decisions.json"))
    requirements = records.read_json(paths.child(run_dir, "requirements.json"))
    records.validate("decision", decisions, "registry")
    records.validate("requirement", requirements, "registry")
    extract = paths.child(run_dir, "extract.json")
    missing = records.read_json(extract).get("input_missing", []) if extract.is_file() else []
    documents = records.read_jsonl(paths.child(run_dir, "documents.jsonl"))
    registry = trace.Registry(decisions["decisions"], requirements["requirements"])
    return registry, missing, documents, decisions, requirements


def registry_facts(registry, documents):
    """{(entity, key): (value, ref)} from the sections each registry entry was read from."""
    sections = {f"{d['id']}#{s['id']}": s for d in documents for s in d["sections"]}
    by_document = {d["id"]: d for d in documents}
    out = {}
    for item in registry.requirements.values():
        if item["basis"] != "explicit" or not item["evidence"]:
            continue
        ref = item["evidence"][0]["ref"]
        section = sections.get(ref)
        for key, value in structure.bullet_facts(section["text"]) if section else []:
            out.setdefault((item["id"], key), (value, f"documents.jsonl#{ref}"))
    for item in registry.decisions.values():
        if item["basis"] != "explicit" or not item["evidence"]:
            continue
        document = by_document.get(item["evidence"][0]["ref"].split("#")[0])
        if document is None:
            continue
        for section in document["sections"]:
            for key, value in structure.bullet_facts(section["text"]):
                ref = f"documents.jsonl#{document['id']}#{section['id']}"
                out.setdefault((item["id"], key), (value, ref))
        for key, value in document.get("front_matter", {}).items():
            if key not in structure.RESERVED_FRONT_MATTER and isinstance(value, str) and value:
                out.setdefault((item["id"], key), (value, f"documents.jsonl#{document['id']}"))
    return out


# The default branch -----------------------------------------------------------------------


class Tree:
    """The readable files of one commit, parsed."""

    def __init__(self, repository, sha):
        self.sha = sha
        self.units, self.kinds, self.unreadable = {}, {}, []
        for path in repository.files(sha):
            kind = structure.file_kind(path)
            if kind is None:
                continue
            text = repository.blob(sha, path)
            if text is None:
                self.unreadable.append(path)
                continue
            self.kinds[path] = kind
            self.units[path] = structure.parse(path, text)

    def addresses(self):
        return sorted(
            unit["node"][1]
            for units in self.units.values()
            for unit in units
            if unit["node"][0] == "iac"
        )

    def file_paths(self):
        return sorted((kind, path) for path, kind in self.kinds.items() if kind in FILE_NODES)


def _entity(registry, written):
    node = registry.resolve(written)
    return node[1] if node else written


def _named(registry, name):
    written = registry.external(name)
    return name if written == name else f"{name} ({written})"


def default_facts(registry, tree):
    """{(entity, key): [(value, ref)]} over the default branch."""
    out = {}
    for path, units in sorted(tree.units.items()):
        for unit in units:
            for written, key, value, line in unit["facts"]:
                ref = f"{tree.sha}:{path}#L{line}"
                out.setdefault((_entity(registry, written), key), []).append((value, ref))
    return out


def _stub_links(data):
    """Fixed rules, no model: a document that contains an entity's title (seven characters or
    more) and not its id is linked to it."""
    links = []
    for document in data["documents"]:
        for entity in data["entities"]:
            title, text = entity["title"], document["text"]
            if title in text and entity["id"] not in text and entity["written"] not in text:
                relation = "refines" if entity["type"] == "requirement" else "depends_on"
                links.append(
                    {
                        "path": document["path"],
                        "to": entity["id"],
                        "relation": relation,
                        "quote": title,
                    }
                )
    return {"links": links, "explanations": []}


def _stub_explain(data):
    return {"links": [], "explanations": []}


def inferred_links(session, registry, tree, texts, rejected):
    if session is None:
        return [], None
    documents = [
        {"path": path, "text": texts[path]}
        for path, units in sorted(tree.units.items())
        if tree.kinds[path] == "markdown"
        and units
        and all(u["node"][0] == "design" for u in units)
        and not any(unit["refs"] for unit in units)
    ]
    titles = registry.titles(TITLE_MINIMUM)
    entities = [
        {
            "id": name,
            "written": registry.external(name),
            "type": "decision" if name in registry.decisions else "requirement",
            "title": title,
        }
        for name, title in sorted(titles.items())
    ]
    if not documents or not entities:
        return [], None
    data = {"documents": documents, "entities": entities, "conflicts": []}
    output = session.ask(STAGE, LINK_INSTRUCTIONS, data, OUTPUT_SCHEMA, _stub_links)
    call = session.last_call_id
    sent = {item["path"]: item["text"] for item in documents}
    kept = []
    for index, proposal in enumerate(output["links"]):
        target = registry.resolve(proposal["to"])
        text = sent.get(proposal["path"])
        reasons = []
        if text is None:
            reasons.append("unknown_document: the path was not sent")
        elif proposal["quote"] not in text:
            reasons.append("quote_not_found: the quote is not in the document")
        if target is None:
            reasons.append(f"unknown_entity: {proposal['to']} is not in the registry")
        if reasons:
            rejected.append({"key": f"links[{index}]", "reasons": reasons, "call_id": call})
            continue
        assert target is not None
        source = ("design", proposal["path"])
        ref = f"llm-calls.jsonl#{call}"
        kept.append(
            trace.link(
                source, target, proposal["relation"], "inferred", ref, proposal["quote"], tree.sha
            )
        )
    return kept, call


# Per PR -----------------------------------------------------------------------------------


def _ranges_all(units):
    return [(unit["start"], unit["end"]) for unit in units]


class PullAnalysis:
    """What one PR changes, read from its diff against the merge base."""

    def __init__(self, pull, merge_base, head):
        self.pull, self.merge_base, self.head = pull, merge_base, head
        self.number = pull["number"]
        self.files = []  # parsed diff entries
        self.changed_units = []  # (path, unit) on either side
        self.links = []  # explicit links of both versions of the changed files
        self.added_links, self.added_dangling = [], []
        self.fact_changes = {}  # (entity, key) -> {"values": set, "evidence": [...]}
        self.tested = set()  # requirement ids named by changed tests
        self.unreadable = []


def analyze_pull(pull, merge_base, head, repository, cache, registry, addresses, file_paths):
    result = PullAnalysis(pull, merge_base, head)
    result.files = git_prs.parse_diff(cache.get(merge_base, head))
    for entry in result.files:
        path, status = entry["path"], entry["status"]
        kind = structure.file_kind(path)
        if kind is None:
            continue
        base_text = "" if status == "A" else repository.blob(merge_base, path)
        head_text = "" if status == "D" else repository.blob(head, path)
        if base_text is None or head_text is None:
            result.unreadable.append(path)
            continue
        base_units = structure.parse(path, base_text)
        head_units = structure.parse(path, head_text)
        removed = (
            _ranges_all(base_units) if status == "D" else git_prs.removed_ranges(entry["base"])
        )
        added = _ranges_all(head_units) if status == "A" else entry["head"]
        changed = structure.touched(base_units, removed) + structure.touched(head_units, added)
        result.changed_units += [(path, kind, unit) for unit in changed]
        base_links, base_dangling = trace.file_links(
            path, base_units, merge_base, registry, addresses, file_paths
        )
        head_links, head_dangling = trace.file_links(
            path, head_units, head, registry, addresses, file_paths
        )
        result.links += base_links + head_links
        before = {(trace.endpoints(item), item["relation"]) for item in base_links}
        result.added_links += [
            item for item in head_links if (trace.endpoints(item), item["relation"]) not in before
        ]
        old = {(item["from"], item["ref"]) for item in base_dangling}
        result.added_dangling += [d for d in head_dangling if (d["from"], d["ref"]) not in old]
        base_facts, _ = structure.facts(base_units)
        head_facts, _ = structure.facts(head_units)
        for written, key in sorted(set(base_facts) | set(head_facts)):
            was = base_facts.get((written, key), (None, None))[0]
            now, line = head_facts.get((written, key), (None, None))
            if was == now:
                continue
            entity = _entity(registry, written)
            change = result.fact_changes.setdefault(
                (entity, key), {"values": set(), "evidence": []}
            )
            change["values"].add(now)
            where = f"{head}:{path}#L{line}" if line else f"{merge_base}:{path}"
            change["evidence"].append((where, was, now))
        if kind == "test":
            for unit in structure.touched(head_units, added):
                for ref in unit["refs"]:
                    node = registry.resolve(ref["id"])
                    if node and node[0] == "requirement":
                        result.tested.add(node[1])
    return result


def _starts(registry, analysis):
    starts = set()
    for path, kind, unit in analysis.changed_units:
        starts.add(registry.node(unit["node"]))
        if kind in FILE_NODES:
            starts.add((kind, path))
    return starts


def pull_findings(analysis, registry, default_links):
    out, number = [], analysis.number
    graph = trace.unique(default_links + analysis.links)
    reached = trace.reach(_starts(registry, analysis), graph)
    entities = {}
    for node, (basis, via) in reached.items():
        if registry.entity(node):
            entities[node[1]] = (basis, via)
    for node in _starts(registry, analysis):
        if registry.entity(node):
            entities[node[1]] = ("explicit", [])
    for entity, key in analysis.fact_changes:
        if entity in registry.index.values():
            entities[entity] = ("explicit", [])
    if entities:
        items = [
            fd.evidence(f"pulls#{number}", "explicit", "changed: " + ", ".join(_changed(analysis)))
        ]
        for name, (basis, via) in sorted(entities.items()):
            ref = " ".join(via) if via else f"pulls#{number}"
            items.append(fd.evidence(ref, basis, f"reaches {name}"))
        out.append(
            fd.draft(
                "impact",
                list(entities),
                [number],
                items,
                f"PR #{number} changes units that trace to {', '.join(sorted(entities))}.",
            )
        )
    for item in analysis.added_links:
        target = (item["to"]["type"], item["to"]["id"])
        source = (item["from"]["type"], item["from"]["id"])
        if target[0] != "decision" or source[0] == "decision":
            continue
        if registry.status(target[1]) != "superseded":
            continue
        successors = registry.decisions[target[1]]["superseded_by"]
        entities_here = [target[1]] + ([source[1]] if registry.entity(source) else [])
        out.append(
            fd.draft(
                "superseded_dependency",
                entities_here,
                [number],
                [
                    fd.evidence(
                        item["evidence"]["ref"], "explicit", item["evidence"].get("quote", "")
                    )
                ],
                f"PR #{number} adds a reference to {target[1]} "
                f"({registry.external(target[1])}), which {', '.join(successors)} superseded.",
            )
        )
    for dangling in analysis.added_dangling:
        out.append(
            fd.draft(
                "input_missing",
                [],
                [number],
                [fd.evidence(dangling["evidence"], "explicit", dangling["quote"])],
                f"PR #{number} refers to {dangling['ref']}, which is not in the registry.",
                missing=dangling["ref"],
            )
        )
    changed_requirements = sorted(
        entity for entity, _ in analysis.fact_changes if entity in registry.requirements
    )
    for name in sorted(set(changed_requirements) - analysis.tested):
        keys = sorted(key for entity, key in analysis.fact_changes if entity == name)
        items = [
            fd.evidence(where, "explicit", f"{key}: {was} -> {now}")
            for (entity, key), change in sorted(
                analysis.fact_changes.items(), key=lambda pair: pair[0]
            )
            if entity == name
            for where, was, now in change["evidence"]
        ]
        out.append(
            fd.draft(
                "missing_test",
                [name],
                [number],
                items,
                f"PR #{number} changes {name} ({', '.join(keys)}) and changes no test whose name "
                f"carries {name}.",
            )
        )
    return out


def _changed(analysis):
    return sorted({entry["path"] for entry in analysis.files})[:20]


def semantic_conflicts(analyses, limitations):
    """Candidates from the (entity, key) index. Returns [(entity, key, [pr numbers], items)]."""
    index = {}
    for analysis in analyses:
        for (entity, key), change in analysis.fact_changes.items():
            index.setdefault((entity, key), []).append((analysis, change))
    by_number = {analysis.number: analysis for analysis in analyses}
    out = []
    for (entity, key), changes in sorted(index.items(), key=lambda pair: pair[0]):
        if len(changes) < 2:
            continue
        involved = set()
        for i, (first, a) in enumerate(changes):
            for second, b in changes[i + 1 :]:
                if a["values"] == b["values"]:
                    continue
                if _textual(first, second):
                    limitations.append(
                        f"PR #{first.number} and PR #{second.number} both change {entity} {key} "
                        "and their diffs overlap on the same base; git reports this as a merge "
                        "conflict, so it is not counted as a semantic conflict."
                    )
                    continue
                involved |= {first.number, second.number}
        if not involved:
            continue
        items = []
        for number in sorted(involved):
            change = dict(changes)[by_number[number]]
            for where, was, now in change["evidence"]:
                items.append(fd.evidence(where, "explicit", f"PR #{number}: {key} {was} -> {now}"))
        values = {
            n: sorted(str(v) for v in dict(changes)[by_number[n]]["values"]) for n in involved
        }
        out.append((entity, key, sorted(involved), items, values))
    return out


def _textual(first, second):
    if first.merge_base != second.merge_base:
        return False
    hunks = {entry["path"]: entry["base"] for entry in first.files}
    for entry in second.files:
        if entry["path"] in hunks and git_prs.overlaps(hunks[entry["path"]], entry["base"]):
            return True
    return False


# Default-branch findings -----------------------------------------------------------------


def branch_findings(registry, tree, links, dangling, facts_registry, facts_branch, missing):
    out = []
    for (entity, key), stated in sorted(facts_branch.items()):
        expected = facts_registry.get((entity, key))
        values = sorted({value for value, _ in stated})
        if expected is not None:
            wrong = [(value, ref) for value, ref in stated if value != expected[0]]
            if not wrong:
                continue
            items = [fd.evidence(expected[1], "explicit", f"registry: {key}: {expected[0]}")]
            items += [fd.evidence(ref, "explicit", f"{key}: {value}") for value, ref in wrong]
            text = f"{entity} {key} is {expected[0]} in the registry and {values} in files."
        elif len(values) > 1:
            items = [fd.evidence(ref, "explicit", f"{key}: {value}") for value, ref in stated]
            text = f"files state {entity} {key} as {values}."
        else:
            continue
        out.append(fd.draft("contradiction", [entity], [], items, text, key=key))

    named = set()
    for units in tree.units.values():
        for unit in units:
            node = registry.node(unit["node"])
            named.add(node[1])
            named |= {_entity(registry, ref["id"]) for ref in unit["refs"]}
    stated_keys = set(facts_branch)
    candidates = [d["id"] for d in registry.decisions.values() if d["status"] == "approved"] + [
        r["id"] for r in registry.requirements.values() if r["basis"] == "explicit"
    ]
    for name in sorted(candidates):
        items = []
        if name not in named:
            items.append(
                fd.evidence(f"{tree.sha}", "explicit", f"no file names {registry.external(name)}")
            )
        for (entity, key), (value, ref) in sorted(facts_registry.items()):
            if entity == name and (entity, key) not in stated_keys:
                items.append(fd.evidence(ref, "explicit", f"{key}: {value} is in no file"))
        if items:
            out.append(
                fd.draft(
                    "not_reflected",
                    [name],
                    [],
                    items,
                    f"{_named(registry, name)} is not reflected in the default branch.",
                )
            )

    for item in links:
        source, target = trace.endpoints(item)
        if target[0] != "decision" or source[0] == "decision":
            continue
        if registry.status(target[1]) != "superseded":
            continue
        successors = registry.decisions[target[1]]["superseded_by"]
        entities = [target[1]] + ([source[1]] if registry.entity(source) else [])
        note = item["evidence"].get("quote", "")
        out.append(
            fd.draft(
                "superseded_dependency",
                entities,
                [],
                [fd.evidence(item["evidence"]["ref"], item["basis"], note)],
                f"{source[1]} depends on {target[1]} ({registry.external(target[1])}), which "
                f"{', '.join(successors)} superseded.",
            )
        )

    seen = set()
    for gap in missing:
        ref = gap.get("ref") or gap.get("locator") or gap.get("source_id") or "unknown"
        if ref in seen:
            continue
        seen.add(ref)
        source = gap.get("from") or gap.get("source_id") or ""
        note = f"{gap.get('kind') or gap.get('reason', 'missing')}: {source} -> {ref}"
        out.append(
            fd.draft(
                "input_missing",
                [source] if str(source).startswith(("REQ-", "DEC-")) else [],
                [],
                [fd.evidence("extract.json#input_missing", "explicit", note)],
                f"{ref} is referenced or listed but could not be found.",
                missing=str(ref),
            )
        )
    for item in dangling:
        if item["ref"] in seen:
            continue
        seen.add(item["ref"])
        out.append(
            fd.draft(
                "input_missing",
                [item["from"][1]] if registry.entity(item["from"]) else [],
                [],
                [fd.evidence(item["evidence"], "explicit", item["quote"])],
                f"{item['ref']} is referenced but not in the registry.",
                missing=item["ref"],
            )
        )

    for item in sorted(registry.requirements.values(), key=lambda r: r["id"]):
        if not item["ambiguity"]:
            continue
        items = [
            fd.evidence(
                f"requirements.json#{item['id']}",
                "explicit" if entry["detector"] == "rule" else "inferred",
                f"{entry['kind']}: {entry.get('span', '')}".strip(": "),
            )
            for entry in item["ambiguity"]
        ]
        kinds = sorted({entry["kind"] for entry in item["ambiguity"]})
        out.append(
            fd.draft(
                "ambiguous_requirement",
                [item["id"]],
                [],
                items,
                f"{item['id']} is ambiguous: {', '.join(kinds)}.",
            )
        )
    return out


# The stage --------------------------------------------------------------------------------


def _explain(session, candidates, titles):
    """{index: (text, call id)} from the backend; the template stays where it gives none."""
    if session is None or not candidates:
        return {}
    conflicts = [
        {
            "key": f"C{index}",
            "entity": entity,
            "structured_key": key,
            "prs": [
                {"number": n, "title": titles.get(n, ""), "values": values[n]} for n in numbers
            ],
        }
        for index, (entity, key, numbers, _, values) in enumerate(candidates, start=1)
    ]
    data = {"documents": [], "entities": [], "conflicts": conflicts}
    output = session.ask(STAGE, EXPLAIN_INSTRUCTIONS, data, OUTPUT_SCHEMA, _stub_explain)
    known = {item["key"] for item in conflicts}
    return {
        int(item["key"][1:]): (item["text"], session.last_call_id)
        for item in output["explanations"]
        if item["key"] in known
    }


def _pr_record(pull, merge_base=None, analyzed=None, stale=False, reason=None):
    record = {
        "number": pull["number"],
        "head_sha": pull.get("head_sha"),
        "merge_base": merge_base,
        "analyzed_head_sha": analyzed,
        "stale": stale,
    }
    if pull.get("title"):
        record["title"] = pull["title"]
    if reason:
        record["missing"] = reason
    return record


def analyze(run_dir, context):
    """Run the stage. `context` carries transport, limiter, repo, source, token, root,
    default_branch, session. Returns (summary, exit code)."""
    run_id, now = run_dir.name, records.iso()
    registry, missing, documents, decisions, requirements = _registry(run_dir)
    owner, name = context["repo"]
    pulls, list_gaps = git_prs.list_open(context["transport"], context["limiter"], owner, name)
    branch = context.get("default_branch") or next(
        (pull["default_branch"] for pull in pulls if pull.get("default_branch")), None
    )
    if branch is None:
        raise InputError("the default branch is unknown: pass --default-branch.")
    branch = git_prs.branch_name(branch)
    repository = git_prs.Repository.open(
        context["root"], owner, name, context["source"], context.get("token")
    )
    fetched = repository.fetch_open(branch, [pull["number"] for pull in pulls])
    main_sha = repository.rev_parse(git_prs.main_ref(branch))
    if main_sha is None:
        raise QualityGateError(f"the default branch {branch!r} has no commit; nothing analyzed.")
    tree = Tree(repository, main_sha)
    texts = {path: repository.blob(main_sha, path) or "" for path in tree.units}
    addresses, file_paths = tree.addresses(), tree.file_paths()
    links, dangling = trace.registry_links(registry), []
    for path, units in sorted(tree.units.items()):
        found, gaps = trace.file_links(path, units, main_sha, registry, addresses, file_paths)
        links += found
        dangling += gaps
    rejected, limitations = [], []
    session = context.get("session")
    inferred, link_call = inferred_links(session, registry, tree, texts, rejected)
    links = trace.unique(links + inferred)
    link_rows = trace.numbered(links, run_id, now, records.SCHEMA_VERSION)
    trace_file = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "default_branch": {"name": branch, "sha": main_sha},
        "links": link_rows,
    }
    records.validate("trace-link", trace_file, "file")
    records.write_json(paths.child(run_dir, "trace.json"), trace_file)

    facts_registry = registry_facts(registry, documents)
    facts_branch = default_facts(registry, tree)
    drafts = branch_findings(
        registry, tree, link_rows, dangling, facts_registry, facts_branch, missing
    )

    cache = git_prs.DiffCache(repository, run_dir)
    pr_records, analyses = [], []
    for gap in list_gaps:
        if gap.get("number"):
            pr_records.append(_pr_record({"number": gap["number"]}, reason=gap["reason"]))
            drafts.append(
                fd.draft(
                    "input_missing",
                    [],
                    [gap["number"]],
                    [fd.evidence(f"pulls#{gap['number']}", "explicit", gap["reason"])],
                    f"PR #{gap['number']} has no usable head SHA in the PR list.",
                    missing=f"pull {gap['number']}",
                )
            )
    for pull in sorted(pulls, key=lambda item: item["number"]):
        number = pull["number"]
        if number not in fetched:
            reason = "head_not_fetched" + (
                "; head repository deleted" if pull["head_repo_missing"] else ""
            )
            pr_records.append(_pr_record(pull, reason=reason))
            drafts.append(
                fd.draft(
                    "input_missing",
                    [],
                    [number],
                    [fd.evidence(f"pulls#{number}", "explicit", reason)],
                    f"the head of PR #{number} could not be fetched; it was not analyzed.",
                    missing=f"refs/pull/{number}/head",
                )
            )
            continue
        head = repository.rev_parse(git_prs.pull_ref(number))
        if head != pull["head_sha"]:
            pr_records.append(_pr_record(pull, analyzed=head, stale=True))
            drafts.append(
                fd.draft(
                    "stale_sha",
                    [],
                    [number],
                    [
                        fd.evidence(
                            f"pulls#{number}", "explicit", f"listed head {pull['head_sha']}"
                        ),
                        fd.evidence(git_prs.pull_ref(number), "explicit", f"fetched head {head}"),
                    ],
                    f"the head of PR #{number} moved between the PR list and the fetch; it was "
                    "not analyzed. Run the analysis again.",
                )
            )
            continue
        merge_base = repository.merge_base(main_sha, head)
        if merge_base is None:
            pr_records.append(_pr_record(pull, analyzed=head, reason="no_merge_base"))
            drafts.append(
                fd.draft(
                    "input_missing",
                    [],
                    [number],
                    [fd.evidence(f"pulls#{number}", "explicit", "no merge base")],
                    f"PR #{number} shares no history with {branch}; it was not analyzed.",
                    missing=f"merge-base {number}",
                )
            )
            continue
        analysis = analyze_pull(
            pull, merge_base, head, repository, cache, registry, addresses, file_paths
        )
        analyses.append(analysis)
        pr_records.append(_pr_record(pull, merge_base, head))
        limitations += [
            f"PR #{number}: {path} is not UTF-8; not read" for path in analysis.unreadable
        ]
        drafts += pull_findings(analysis, registry, link_rows)

    candidates = semantic_conflicts(analyses, limitations)
    titles = {pull["number"]: pull["title"] for pull in pulls}
    explained = _explain(session, candidates, titles)
    for index, (entity, key, numbers, items, values) in enumerate(candidates, start=1):
        listed = "; ".join(f"#{n}: {', '.join(values[n])}" for n in numbers)
        text, by = explained.get(index, (None, "template"))
        text = text or (
            f"{len(numbers)} PRs set {entity} {key} to different values ({listed}); their diffs "
            "do not overlap, so git merges them without a conflict and the values disagree."
        )
        drafts.append(
            fd.draft("semantic_conflict", [entity], numbers, items, text, key=key, explained_by=by)
        )

    limitations += [f"{path} on {branch} is not UTF-8; not read" for path in tree.unreadable]
    found = fd.numbered(drafts)
    problems = fd.check(found)
    if problems:
        raise QualityGateError("the findings break their invariants: " + "; ".join(problems[:5]))
    stale = [record["number"] for record in pr_records if record["stale"]]
    unanalyzed = [record["number"] for record in pr_records if record.get("missing")]
    status = "stale" if stale else ("partial" if unanalyzed else "complete")
    head = {"schema_version": records.SCHEMA_VERSION, "run_id": run_id, "created_at": now}
    findings_file = {
        **head,
        "status": status,
        "repo": f"{owner}/{name}",
        "default_branch": {"name": branch, "sha": main_sha},
        "criteria_hash": criteria_hash(decisions, requirements),
        "prs": sorted(pr_records, key=lambda record: record["number"]),
        "findings": found,
        "limitations": limitations,
    }
    records.validate("impact-report", findings_file, "findings_file")
    records.write_json(paths.child(run_dir, "findings.json"), findings_file)
    counts = {kind: 0 for kind in fd.KINDS}
    for item in found:
        counts[item["kind"]] += 1
    summary = {
        "run_id": run_id,
        "status": status,
        "prs": len(pr_records),
        "stale": stale,
        "unanalyzed": unanalyzed,
        "findings": counts,
        "links": {
            "explicit": sum(1 for item in link_rows if item["basis"] == "explicit"),
            "inferred": sum(1 for item in link_rows if item["basis"] == "inferred"),
        },
        "git_calls": repository.calls,
        "diffs": {"computed": cache.computed, "reused": cache.reused},
    }
    records.write_json(
        paths.child(run_dir, "analyze.json"),
        {
            **head,
            **summary,
            "backend": session.backend if session else None,
            "link_call": link_call,
            "rejected_links": rejected,
        },
    )
    return summary, (1 if status != "complete" else 0)


def criteria_hash(decisions, requirements):
    """What the findings were judged by: the rule version, the severity table and the cap, and
    the registries."""
    return records.fingerprint(
        {
            "rules_version": RULES_VERSION,
            "severity": fd.SEVERITY,
            "inferred_cap": fd.INFERRED_CAP,
            "decisions": records.fingerprint(decisions["decisions"]),
            "requirements": records.fingerprint(requirements["requirements"]),
        }
    )
