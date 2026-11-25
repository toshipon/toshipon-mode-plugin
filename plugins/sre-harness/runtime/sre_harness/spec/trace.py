"""Trace links: Decision -> Requirement -> Design -> IaC -> Test.

`basis: explicit` links come from fixed rules only:

- the registry: a requirement's `decision_refs` (requirement refines decision);
- an ID written in a unit of a default-branch file (see `structure`): the unit's node points at
  the ID's entity;
- a resource address (`aws_s3_bucket.logs`) or a repository path (`infra/logs.tf`) written in
  a Markdown section: the IaC block or file, or the test file, points at that section's node;
- an ID in a test's name: the test verifies the entity.

`basis: inferred` links are proposals from the backend (or its stub) that passed the checks in
`analyze`. They are listed apart, have `confidence: low`, and a finding that rests on them alone
is never `critical` (see `findings.cap`).

Links point from the lower layer to the upper one; following them from a changed unit reaches
the decisions and requirements it serves.
"""

from collections import deque

from .structure import id_type

NODE_TYPES = ("decision", "requirement", "design", "iac", "test")
RELATIONS = {
    ("requirement", "decision"): "refines",
    ("requirement", "requirement"): "depends_on",
    ("design", "requirement"): "refines",
    ("design", "decision"): "depends_on",
    ("design", "design"): "depends_on",
    ("decision", "decision"): "depends_on",
    ("decision", "requirement"): "depends_on",
    ("iac", "requirement"): "implements",
    ("iac", "decision"): "implements",
    ("iac", "design"): "implements",
    ("iac", "iac"): "depends_on",
    ("test", "requirement"): "verifies",
    ("test", "decision"): "verifies",
    ("test", "design"): "verifies",
    ("test", "iac"): "verifies",
    ("requirement", "design"): "depends_on",
    ("decision", "design"): "depends_on",
}


class Registry:
    """The registries as the analysis reads them: ids, written ids, status, titles."""

    def __init__(self, decisions, requirements):
        self.decisions = {item["id"]: item for item in decisions}
        self.requirements = {item["id"]: item for item in requirements}
        self.index = {}
        for item in [*decisions, *requirements]:
            self.index[item["id"]] = item["id"]
        for item in [*decisions, *requirements]:
            if item.get("external_id"):
                self.index.setdefault(item["external_id"], item["id"])

    def resolve(self, written):
        """The registry node of a written id, or None when the registry does not have it."""
        name = self.index.get(written)
        if name is None:
            return None
        return ("decision", name) if name in self.decisions else ("requirement", name)

    def node(self, node):
        """A unit's node with its written id replaced by the registry id when there is one."""
        if node[0] in ("decision", "requirement"):
            return self.resolve(node[1]) or (id_type(node[1]), node[1])
        return node

    def entity(self, node):
        return node[0] in ("decision", "requirement") and node[1] in self.index.values()

    def status(self, name):
        item = self.decisions.get(name)
        return item["status"] if item else None

    def external(self, name):
        item = self.decisions.get(name) or self.requirements.get(name) or {}
        return item.get("external_id", name)

    def titles(self, minimum):
        """{registry id: title} for titles of at least `minimum` characters."""
        out = {}
        for item in [*self.decisions.values(), *self.requirements.values()]:
            title = (item.get("title") or "").strip()
            if len(title) >= minimum:
                out[item["id"]] = title
        return out


def link(source, target, relation, basis, ref, quote="", sha=None, confidence=None):
    """A link draft without an id. `sha` is the commit a file node was read at."""

    def end(node):
        value = {"type": node[0], "id": node[1]}
        if sha is not None and node[0] in ("design", "iac", "test"):
            value["sha"] = sha
        return value

    evidence = {"ref": ref}
    if quote:
        evidence["quote"] = quote[:300]
    return {
        "from": end(source),
        "to": end(target),
        "relation": relation,
        "basis": basis,
        "evidence": evidence,
        "confidence": confidence or ("high" if basis == "explicit" else "low"),
    }


def endpoints(item):
    return (item["from"]["type"], item["from"]["id"]), (item["to"]["type"], item["to"]["id"])


def _relation(source, target, hint):
    if hint == "supersedes" and source[0] == target[0] == "decision":
        return "conflicts_with"
    return RELATIONS.get((source[0], target[0]), "depends_on")


def _mentioned(text, needle):
    start = text.find(needle)
    while start >= 0:
        before = text[start - 1] if start else " "
        after = text[start + len(needle)] if start + len(needle) < len(text) else " "
        if not (before.isalnum() or before in "_./-") and not (after.isalnum() or after in "_"):
            return True
        start = text.find(needle, start + 1)
    return False


def file_links(path, units, sha, registry, addresses=(), paths=()):
    """(explicit links, dangling references) of one file version.

    `addresses` are the resource addresses and `paths` the IaC and test paths of the tree
    (`[(node type, path)]`); a Markdown section that names one is pointed at by it.
    """
    links, dangling = [], []
    markdown = path.endswith((".md", ".markdown"))
    for unit in units:
        source = registry.node(unit["node"])
        for ref in unit["refs"]:
            target = registry.resolve(ref["id"])
            evidence = f"{sha}:{path}#L{ref['line']}"
            if target is None:
                dangling.append(
                    {"from": source, "ref": ref["id"], "evidence": evidence, "quote": ref["quote"]}
                )
                continue
            if target != source:
                relation = _relation(source, target, ref["hint"])
                links.append(
                    link(source, target, relation, "explicit", evidence, ref["quote"], sha)
                )
        if not markdown:
            continue
        for address in addresses:
            if _mentioned(unit["text"], address):
                target = ("iac", address)
                evidence = f"{sha}:{path}#L{unit['start']}-L{unit['end']}"
                relation = RELATIONS.get(("iac", source[0]), "depends_on")
                links.append(link(target, source, relation, "explicit", evidence, address, sha))
        for kind, other in paths:
            if other != path and _mentioned(unit["text"], other):
                evidence = f"{sha}:{path}#L{unit['start']}-L{unit['end']}"
                relation = RELATIONS.get((kind, source[0]), "depends_on")
                links.append(
                    link((kind, other), source, relation, "explicit", evidence, other, sha)
                )
    return links, dangling


def registry_links(registry):
    out = []
    for item in registry.requirements.values():
        for name in item["decision_refs"]:
            source, target = ("requirement", item["id"]), ("decision", name)
            ref = f"requirements.json#{item['id']}"
            out.append(link(source, target, "refines", "explicit", ref, "decision_refs"))
    return out


def unique(links):
    """Drop repeats of (from, to, relation, basis); an inferred link that repeats an explicit
    one is dropped too. Order is kept."""
    seen, explicit, out = set(), set(), []
    for item in links:
        if item["basis"] == "explicit":
            explicit.add(endpoints(item))
    for item in links:
        ends = endpoints(item)
        key = (*ends, item["relation"], item["basis"])
        if key in seen or (item["basis"] == "inferred" and ends in explicit):
            continue
        seen.add(key)
        out.append(item)
    return out


def numbered(links, run_id, created_at, schema_version):
    rows = []
    ordered = sorted(
        links,
        key=lambda item: (
            item["basis"] != "explicit",
            item["from"]["type"],
            item["from"]["id"],
            item["to"]["type"],
            item["to"]["id"],
            item["relation"],
        ),
    )
    for number, item in enumerate(ordered, start=1):
        rows.append(
            {
                "schema_version": schema_version,
                "id": f"TL-{number:04d}",
                "run_id": run_id,
                "created_at": created_at,
                **item,
            }
        )
    return rows


def reach(starts, links):
    """{node: (basis, [link ids])} for every node reachable from `starts` along links.

    A node is `explicit` when a path of explicit links reaches it; otherwise `inferred`.
    `conflicts_with` (one decision superseding another) is not followed: a change that serves
    the new decision does not reach the one it replaced.
    """

    def walk(allowed):
        edges = {}
        for item in links:
            if item["basis"] in allowed and item["relation"] != "conflicts_with":
                source, target = endpoints(item)
                edges.setdefault(source, []).append((target, item.get("id")))
        found = {}
        queue = deque((node, []) for node in starts)
        visited = set(starts)
        while queue:
            node, via = queue.popleft()
            for target, name in edges.get(node, []):
                if target in visited:
                    continue
                visited.add(target)
                found[target] = via + ([name] if name else [])
                queue.append((target, found[target]))
        return found

    explicit = walk({"explicit"})
    out = {node: ("explicit", via) for node, via in explicit.items()}
    for node, via in walk({"explicit", "inferred"}).items():
        out.setdefault(node, ("inferred", via))
    return out
