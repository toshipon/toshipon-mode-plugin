"""The IaC delivery contract as data, and the check of one delivery record against it.

The harness never writes or advances a delivery. It reads the record a delivery process kept
(`--delivery`), the files its evidence points at (`--evidence`), and reports every violation.
The record's shape is the JSON Schema `iac-delivery`; the order of states, the evidence each
state needs and the cross-transition rules are the tables below. No model is asked.
"""

import json
import unicodedata

from .. import records
from . import evidence as evidence_files

STATES = (
    "requirement_approved",
    "designed",
    "implemented",
    "staging_verified",
    "pr_opened",
    "privileged_approved",
    "deployed",
    "rejected",
    "rolled_back",
)
FORWARD = STATES[:7]
START = FORWARD[0]

# Legal (from, to) pairs. `None` is "no record yet". A change moves one stage forward at a
# time; it can be rejected at any stage before it is deployed, and rolled back once deployed.
TRANSITIONS = frozenset(
    {(None, START)}
    | set(zip(FORWARD, FORWARD[1:]))
    | {(state, "rejected") for state in FORWARD[:-1]}
    | {("deployed", "rolled_back")}
)

# The evidence kinds a transition into each state must carry.
REQUIRED_EVIDENCE = {
    "requirement_approved": frozenset({"requirement"}),
    "designed": frozenset({"requirement", "design"}),
    "implemented": frozenset({"commit"}),
    "staging_verified": frozenset({"plan", "test_result"}),
    "pr_opened": frozenset({"pull_request"}),
    "privileged_approved": frozenset({"plan"}),
    "deployed": frozenset({"execution_record"}),
    "rejected": frozenset({"note"}),
    "rolled_back": frozenset({"execution_record"}),
}

PRIVILEGED_ROLE = "privileged"


def person(name):
    """The key two actor names are compared by: NFKC, trimmed, casefolded, the part before
    `@`, trailing dots dropped. Names are what the record says; nobody's identity is checked."""
    key = unicodedata.normalize("NFKC", name).strip().casefold()
    return key.partition("@")[0].rstrip(".")


def violation(code, transition, message, locator=None):
    item = {"code": code, "transition": transition, "message": message}
    if locator is not None:
        item["locator"] = locator
    return item


def _last(transitions, index, state):
    """The latest transition into `state` before `index`, or None."""
    for earlier in reversed(transitions[:index]):
        if earlier["to"] == state:
            return earlier
    return None


def _hashes(item, kind):
    return {entry["sha256"] for entry in item["evidence"] if entry["kind"] == kind}


def _contents(checked, index, kind, definition):
    """(entry, parsed value) for each verified file of `kind`; shape problems as violations."""
    found, problems = [], []
    for entry, data in checked[index]:
        if entry["kind"] != kind or data is None:
            continue
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            value = None
        if value is None or records.violations("iac-delivery", value, definition):
            problems.append(
                violation(
                    "evidence_content_invalid",
                    index,
                    f"{kind} evidence is not a {definition} JSON object.",
                    entry["locator"],
                )
            )
            continue
        found.append((entry, value))
    return found, problems


# Rules that look past one transition, keyed by the state entered. -------------------------


def _requirement_is_approved(transitions, index, checked):
    found, problems = _contents(checked, index, "requirement", "requirement_evidence")
    for entry, value in found:
        if value["status"] != "approved":
            problems.append(
                violation(
                    "requirement_not_approved",
                    index,
                    f"{value['id']} is {value['status']!r}, not approved.",
                    entry["locator"],
                )
            )
    return problems


def _approver_is_privileged_and_independent(transitions, index, checked):
    actor, problems = transitions[index]["actor"], []
    if actor["role"] != PRIVILEGED_ROLE:
        problems.append(
            violation(
                "approver_not_privileged",
                index,
                f"{actor['name']} approved with role {actor['role']!r}; "
                f"the role must be {PRIVILEGED_ROLE!r}.",
            )
        )
    implemented = _last(transitions, index, "implemented")
    if implemented is not None and person(implemented["actor"]["name"]) == person(actor["name"]):
        problems.append(
            violation(
                "approver_is_implementer",
                index,
                f"{actor['name']} implemented the change and also approved it.",
            )
        )
    return problems


def _approved_plan_was_staged(transitions, index, checked):
    staging = _last(transitions, index, "staging_verified")
    staged = _hashes(staging, "plan") if staging else set()
    unstaged = sorted(_hashes(transitions[index], "plan") - staged)
    if not unstaged:
        return []
    where = "the staging_verified plan" if staging else "any staging_verified transition"
    return [
        violation("plan_not_staged", index, f"approved plan {digest} is not in {where}.")
        for digest in unstaged
    ]


def _execution_record_is_shaped(transitions, index, checked):
    return _contents(checked, index, "execution_record", "execution_record")[1]


def _deployed_the_approved_plan(transitions, index, checked):
    approval = _last(transitions, index, "privileged_approved")
    approved = _hashes(approval, "plan") if approval else set()
    found, problems = _contents(checked, index, "execution_record", "execution_record")
    for entry, value in found:
        if value["plan_sha256"] not in approved:
            problems.append(
                violation(
                    "plan_hash_mismatch",
                    index,
                    f"the execution record ran plan {value['plan_sha256']}, not the plan "
                    "approved at privileged_approved.",
                    entry["locator"],
                )
            )
    return problems


RULES = {
    "requirement_approved": (_requirement_is_approved,),
    "designed": (_requirement_is_approved,),
    "privileged_approved": (_approver_is_privileged_and_independent, _approved_plan_was_staged),
    "deployed": (_deployed_the_approved_plan,),
    "rolled_back": (_execution_record_is_shaped,),
}


# The check -------------------------------------------------------------------------------


def _order(transitions, index):
    item = transitions[index]
    if index == 0:
        if (item["from"], item["to"]) != (None, START):
            return [
                violation(
                    "bad_start",
                    0,
                    f"the first transition must be null -> {START}, "
                    f"not {item['from']} -> {item['to']}.",
                )
            ]
        return []
    previous, problems = transitions[index - 1], []
    if item["from"] != previous["to"]:
        problems.append(
            violation(
                "broken_chain",
                index,
                f"starts from {item['from']} but the previous transition ended in "
                f"{previous['to']}.",
            )
        )
    pair = (item["from"], item["to"])
    if pair not in TRANSITIONS:
        source, target = pair
        if source in FORWARD and target in FORWARD:
            ahead = FORWARD.index(target) > FORWARD.index(source)
            code = "skipped_stage" if ahead else "went_back"
        else:
            code = "illegal_transition"
        problems.append(violation(code, index, f"{source} -> {target} is not a legal transition."))
    if item["at"] < previous["at"]:
        problems.append(
            violation(
                "time_went_back", index, f"{item['at']} is before the previous {previous['at']}."
            )
        )
    return problems


def _evidence(transitions, index, root, cache):
    """Violations of one transition's evidence, and [(entry, verified bytes or None)]."""
    item, problems, checked = transitions[index], [], []
    kinds = {entry["kind"] for entry in item["evidence"]}
    for kind in sorted(REQUIRED_EVIDENCE[item["to"]] - kinds):
        problems.append(
            violation("missing_evidence", index, f"{item['to']} needs {kind} evidence.")
        )
    for entry in item["evidence"]:
        locator = entry["locator"]
        if locator not in cache:
            cache[locator] = evidence_files.read(root, locator)
        data, code = cache[locator]
        if code is None and records.sha256(data) != entry["sha256"]:
            data, code = None, "evidence_hash_mismatch"
        if code is not None:
            problems.append(violation(code, index, f"{entry['kind']} evidence: {code}.", locator))
        checked.append((entry, data))
    return problems, checked


def check(delivery, root, plan_sha256=None):
    """Every contract violation of one delivery record, in transition order. Empty is valid.

    `root` is the --evidence directory. `plan_sha256`, when given, must be the last plan the
    delivery recorded: the plan whose risk is classified is the plan that was delivered.
    """
    problems = records.violations("iac-delivery", delivery)
    if problems:
        return [violation("schema", None, problem) for problem in problems]
    transitions, found, cache, checked = delivery["transitions"], [], {}, []
    for index in range(len(transitions)):
        found += _order(transitions, index)
        problems, verified = _evidence(transitions, index, root, cache)
        found += problems
        checked.append(verified)
    for index, item in enumerate(transitions):
        for rule in RULES.get(item["to"], ()):
            found += rule(transitions, index, checked)
    found.sort(key=lambda item: -1 if item["transition"] is None else item["transition"])
    if delivery["state"] != transitions[-1]["to"]:
        found.append(
            violation(
                "state_mismatch",
                None,
                f"state is {delivery['state']} but the last transition ends in "
                f"{transitions[-1]['to']}.",
            )
        )
    delivered = delivered_plan(delivery)
    if plan_sha256 is not None and delivered is not None and delivered != plan_sha256:
        found.append(
            violation(
                "plan_mismatch",
                None,
                f"--plan is {plan_sha256}; the delivery's last plan is {delivered}.",
            )
        )
    return found


def delivered_plan(delivery):
    """The sha256 of the last plan the delivery recorded, or None (no plan yet, or the record
    does not fit the schema). A --plan is bound to the delivery only when it is this plan."""
    if records.violations("iac-delivery", delivery):
        return None
    plans = [
        entry["sha256"]
        for item in delivery["transitions"]
        for entry in item["evidence"]
        if entry["kind"] == "plan"
    ]
    return plans[-1] if plans else None
