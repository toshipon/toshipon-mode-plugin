"""The Decision Registry: supersession, the effective status, the active set, the invariants.

The registry is the source of truth; documents are views of it. `build` turns decision drafts
(status as the source stated it, `supersedes` as the source wrote it) into stored decisions.

References are resolved by who wrote them. An explicit decision's `supersedes` comes from a
document and names the id a document writes (`external_id`, such as `ADR-0002`, or `DEC-0002`
when the ADR writes that); the registry ids the harness assigns are never a document's target.
An inferred decision's `supersedes` comes from the backend, which was shown the registry ids of
the explicit decisions, and names those. Nothing reaches an inferred decision, so an inferred
edge can never close a cycle. Two decisions that write the same id stop `build` with exit 2. `check` is the invariant checker; `build`'s output always passes it,
and the tests hold registries the builder would never produce to it as well.

Invariants:

1. `superseded` only when `superseded_by` is non-empty and every decision in it was approved
   (status `approved`, or `superseded` itself: in a chain A <- B <- C, B superseded A while it
   was in force). A rejected, proposed or unresolved decision supersedes nothing.
2. `approved` and `superseded` need at least one approver and one piece of evidence.
3. An `inferred` decision is never approved or superseded; a person makes it explicit first.
4. No supersession cycle: `build` refuses one with exit 2, naming the cycle and its documents.
5. Every id in `supersedes` and `superseded_by` exists. A stated reference to a decision that
   is not in the registry is dropped from `supersedes` and recorded as `input_missing`.

Only approved decisions that nothing supersedes are active.
"""

from ..errors import InputError

IN_FORCE = ("approved", "superseded")  # statuses that were approved at some point


def _where(draft, sources):
    names = [draft.get("external_id"), (sources or {}).get(draft["id"])]
    return f"{draft['id']} ({', '.join(name for name in names if name) or draft['id']})"


def _indexes(drafts, sources=None):
    """({written id: registry id}, {registry id: registry id}) over the explicit decisions."""
    written, assigned = {}, {}
    for draft in drafts:
        if draft["basis"] != "explicit":
            continue
        assigned[draft["id"]] = draft["id"]
        name = draft.get("external_id")
        if not name:
            continue
        if name in written:
            first = next(d for d in drafts if d["id"] == written[name])
            raise InputError(
                f"two decisions write the id {name}: {_where(first, sources)} and "
                f"{_where(draft, sources)}; fix the documents. Nothing was written."
            )
        written[name] = draft["id"]
    return written, assigned


def _cycle(edges):
    """A cycle in `edges` (id -> ids it supersedes) as a list of ids, or None."""
    claimed_by = {node: [] for node in edges}
    remaining = {node: 0 for node in edges}
    for node, targets in edges.items():
        for target in targets:
            remaining[target] += 1
            claimed_by[target].append(node)
    ready = [node for node, count in remaining.items() if count == 0]
    order = []
    while ready:
        node = ready.pop()
        order.append(node)
        for target in edges[node]:
            remaining[target] -= 1
            if remaining[target] == 0:
                ready.append(target)
    left = {node for node in edges if node not in order}
    if not left:
        return None, order
    # Every node left has a claimer that is also left: walk claimers back until one repeats.
    node, path = min(left), []
    while node not in path:
        path.append(node)
        node = min(claimer for claimer in claimed_by[node] if claimer in left)
    cycle = path[path.index(node) :]
    return list(reversed(cycle)) + [cycle[-1]], order


def _issue(kind, detail):
    return {"kind": kind, "detail": detail[:300]}


def build(drafts, sources=None):
    """{decisions, active, input_missing} from drafts. Raises InputError on a cycle or on a
    written id two decisions claim. `sources` ({registry id: document path}) names the
    documents in those messages."""
    written, assigned = _indexes(drafts, sources)
    by_id = {draft["id"]: draft for draft in drafts}
    edges, missing = {}, []
    for draft in drafts:
        resolved = []
        index = written if draft["basis"] == "explicit" else assigned
        for ref in draft["supersedes"]:
            target = index.get(ref)
            if target is None:
                missing.append({"kind": "decision_ref", "from": draft["id"], "ref": ref})
            elif target not in resolved:
                resolved.append(target)
        edges[draft["id"]] = resolved
    cycle, order = _cycle(edges)
    if cycle is not None:
        raise InputError(
            "decisions supersede each other in a cycle: "
            + " -> ".join(_where(by_id[node], sources) for node in cycle)
            + "; fix the documents. Nothing was written."
        )
    claimers = {node: [] for node in edges}
    for node, targets in edges.items():
        for target in targets:
            claimers[target].append(node)

    out = {}
    for node in order:  # every claimer comes before the decisions it claims
        draft = by_id[node]
        stated, issues = draft["status"], list(draft.get("issues", []))
        successors = sorted(c for c in claimers[node] if out[c]["status"] in IN_FORCE)
        status = stated
        if stated in IN_FORCE:
            if draft["basis"] == "inferred":
                status = "unresolved"
                issues.append(_issue("inferred_not_approvable", "inferred; a person must confirm"))
            elif not draft["approvers"] or not draft["evidence"]:
                status = "unresolved"
                issues.append(
                    _issue("approval_incomplete", f"stated {stated} without approver or evidence")
                )
            elif successors:
                status = "superseded"
            elif stated == "superseded":
                status = "unresolved"
                issues.append(
                    _issue(
                        "no_approved_successor",
                        "stated superseded, but no approved decision supersedes it",
                    )
                )
        out[node] = {
            **draft,
            "status": status,
            "stated_status": draft.get("stated_status", stated),
            "supersedes": edges[node],
            "superseded_by": successors if status == "superseded" else [],
            "issues": issues,
        }
        for claimer in claimers[node]:
            if out[claimer]["status"] not in IN_FORCE:
                out[claimer]["issues"].append(
                    _issue(
                        "cannot_supersede",
                        f"{out[claimer]['status']}: does not supersede {node}",
                    )
                )
    decisions = [out[draft["id"]] for draft in drafts]
    return {"decisions": decisions, "active": active(decisions), "input_missing": missing}


def active(decisions):
    return sorted(item["id"] for item in decisions if item["status"] == "approved")


def check(decisions):
    """Invariant violations, empty when the registry holds."""
    problems = []
    by_id = {}
    for item in decisions:
        if item["id"] in by_id:
            problems.append(f"{item['id']}: the id is used twice")
        by_id[item["id"]] = item
    for item in decisions:
        name, status = item["id"], item["status"]
        for field in ("supersedes", "superseded_by"):
            for ref in item[field]:
                if ref not in by_id:
                    problems.append(f"{name}: {field} names {ref}, which is not in the registry")
        if status in IN_FORCE and (not item["approvers"] or not item["evidence"]):
            problems.append(f"{name}: {status} needs an approver and evidence")
        if status in IN_FORCE and item["basis"] == "inferred":
            problems.append(f"{name}: an inferred decision cannot be {status}")
        if status == "superseded" and not item["superseded_by"]:
            problems.append(f"{name}: superseded but superseded_by is empty")
        if status != "superseded" and item["superseded_by"]:
            problems.append(f"{name}: superseded_by is set but the status is {status}")
        for ref in item["superseded_by"]:
            successor = by_id.get(ref)
            if successor is None:
                continue
            if successor["status"] not in IN_FORCE:
                problems.append(
                    f"{name}: superseded by {ref}, which is {successor['status']}, not approved"
                )
            if name not in successor["supersedes"]:
                problems.append(f"{name}: superseded by {ref}, which does not supersede it")
    edges = {item["id"]: [ref for ref in item["supersedes"] if ref in by_id] for item in decisions}
    cycle, _ = _cycle(edges)
    if cycle is not None:
        problems.append("supersession cycle: " + " -> ".join(cycle))
    return problems
