"""The IaC delivery contract: legal transitions, required evidence, evidence files."""

import itertools
import os

import pytest

from examples.synthetic import iac_scenarios as sc
from sre_harness import records
from sre_harness.iac import contract, evidence

FORWARD = sc.STAGES


def codes(found):
    return [item["code"] for item in found]


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "evidence"
    return root, sc.valid_delivery(root)


def check(delivery, root, **options):
    return contract.check(delivery, root, **options)


# The tables ---------------------------------------------------------------------------


def test_the_forward_chain_and_the_exits_are_the_only_legal_transitions():
    chain = {(None, FORWARD[0])} | set(zip(FORWARD, FORWARD[1:]))
    exits = {(state, "rejected") for state in FORWARD[:-1]} | {("deployed", "rolled_back")}
    assert contract.TRANSITIONS == chain | exits
    assert set(contract.STATES) == set(FORWARD) | {"rejected", "rolled_back"}
    assert contract.STATES == tuple(records.schema("iac-delivery")["$defs"]["state"]["enum"])


def test_every_state_names_its_required_evidence():
    assert set(contract.REQUIRED_EVIDENCE) == set(contract.STATES)
    assert contract.REQUIRED_EVIDENCE["designed"] >= {"requirement"}
    assert contract.REQUIRED_EVIDENCE["staging_verified"] >= {"plan", "test_result"}
    assert contract.REQUIRED_EVIDENCE["privileged_approved"] >= {"plan"}
    assert contract.REQUIRED_EVIDENCE["deployed"] >= {"execution_record"}


@pytest.mark.parametrize("until", FORWARD)
def test_a_correct_chain_passes_at_every_stage(tmp_path, until):
    root = tmp_path / "evidence"
    delivery = sc.valid_delivery(root, until=until)
    assert records.violations("iac-delivery", delivery) == []
    assert check(delivery, root) == []


@pytest.mark.parametrize("stage", FORWARD[:-1])
def test_a_change_can_be_rejected_before_it_is_deployed(tmp_path, stage):
    root = tmp_path / "evidence"
    delivery = sc.valid_delivery(root, until=stage)
    note = sc.evidence_item(root, "note", "notes/rejection.md")
    delivery["transitions"].append(
        {
            "from": stage,
            "to": "rejected",
            "at": "2026-02-01T00:00:00Z",
            "actor": {"name": "bob", "role": "privileged"},
            "evidence": [note],
        }
    )
    delivery["state"] = "rejected"
    assert check(delivery, root) == []


def test_a_deployed_change_can_be_rolled_back(case):
    root, delivery = case
    record = sc.evidence_item(root, "execution_record", "deploy/execution.json")
    delivery["transitions"].append(
        {
            "from": "deployed",
            "to": "rolled_back",
            "at": "2026-01-13T00:00:00Z",
            "actor": {"name": "bob", "role": "privileged"},
            "evidence": [record],
        }
    )
    delivery["state"] = "rolled_back"
    assert check(delivery, root) == []


def _illegal_pairs():
    for source, target in itertools.product((None, *contract.STATES), contract.STATES):
        if (source, target) not in contract.TRANSITIONS:
            yield source, target


def _expected(source, target):
    if source is None:
        return "bad_start"
    if source in FORWARD and target in FORWARD:
        return "skipped_stage" if FORWARD.index(target) > FORWARD.index(source) else "went_back"
    return "illegal_transition"


@pytest.mark.parametrize("source, target", list(_illegal_pairs()))
def test_every_pair_outside_the_table_is_a_violation(case, source, target):
    root, delivery = case
    if source is None:
        delivery["transitions"] = [{**delivery["transitions"][0], "to": target}]
    else:
        delivery["transitions"] = delivery["transitions"][:1]
        delivery["transitions"][0]["to"] = source
        delivery["transitions"][0]["from"] = None
        delivery["transitions"].append(
            {
                "from": source,
                "to": target,
                "at": "2026-02-01T00:00:00Z",
                "actor": {"name": "bob", "role": "privileged"},
                "evidence": [],
            }
        )
    delivery["state"] = target
    assert _expected(source, target) in codes(check(delivery, root))


def test_skipping_staging_and_going_back_are_named(case):
    root, delivery = case
    skipped = sc.broken_delivery(root, "skipped_stage")
    assert "skipped_stage" in codes(check(skipped, root))
    back = sc.valid_delivery(root, until="staging_verified")
    back["transitions"].append({**back["transitions"][2], "from": "staging_verified"})
    back["transitions"][-1]["at"] = "2026-01-10T00:00:00Z"
    back["state"] = "implemented"
    assert "went_back" in codes(check(back, root))


def test_an_undefined_state_is_refused_by_the_schema(case):
    root, delivery = case
    delivery["transitions"][-1]["to"] = "applied"
    delivery["state"] = "applied"
    found = check(delivery, root)
    assert codes(found) and set(codes(found)) == {"schema"}
    assert any("applied" in item["message"] for item in found)


def test_the_state_must_be_the_last_transitions_target(case):
    root, delivery = case
    delivery["state"] = "privileged_approved"
    assert codes(check(delivery, root)) == ["state_mismatch"]


def test_each_transition_starts_where_the_previous_one_ended(case):
    root, delivery = case
    delivery["transitions"][3]["from"] = "designed"
    assert "broken_chain" in codes(check(delivery, root))


def test_time_must_not_go_back(case):
    root, _ = case
    delivery = sc.broken_delivery(root, "time_went_back")
    found = check(delivery, root)
    assert "time_went_back" in codes(found)
    (item,) = [item for item in found if item["code"] == "time_went_back"]
    assert item["transition"] == 5


def test_equal_times_are_allowed(case):
    root, delivery = case
    delivery["transitions"][4]["at"] = delivery["transitions"][3]["at"]
    assert check(delivery, root) == []


# Required evidence ------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage, kind",
    [(stage, kind) for stage in FORWARD for kind in sorted(contract.REQUIRED_EVIDENCE[stage])],
)
def test_missing_evidence_is_a_violation(case, stage, kind):
    root, delivery = case
    index = FORWARD.index(stage)
    item = delivery["transitions"][index]
    item["evidence"] = [entry for entry in item["evidence"] if entry["kind"] != kind]
    found = check(delivery, root)
    assert {"code": "missing_evidence", "transition": index} in [
        {"code": entry["code"], "transition": entry["transition"]} for entry in found
    ]
    assert any(kind in entry["message"] for entry in found if entry["code"] == "missing_evidence")


def test_the_requirement_must_be_approved(case):
    root, delivery = case
    (root / "requirement/REQ-0001.json").write_text('{"id": "REQ-0001", "status": "draft"}\n')
    for item in delivery["transitions"]:
        for entry in item["evidence"]:
            if entry["kind"] == "requirement":
                entry["sha256"] = sc.sha256_file(root / entry["locator"])
    found = codes(check(delivery, root))
    assert found.count("requirement_not_approved") == 2


def test_the_approver_must_not_be_the_implementer(case):
    root, _ = case
    delivery = sc.broken_delivery(root, "approver_is_implementer")
    assert codes(check(delivery, root)) == ["approver_is_implementer"]


def test_the_approver_must_hold_the_privileged_role(case):
    root, _ = case
    delivery = sc.broken_delivery(root, "approver_not_privileged")
    assert codes(check(delivery, root)) == ["approver_not_privileged"]


def test_the_deployed_plan_must_be_the_approved_plan(case):
    root, _ = case
    delivery = sc.broken_delivery(root, "deployed_other_plan")
    assert codes(check(delivery, root)) == ["plan_hash_mismatch"]


def test_the_approved_plan_must_be_the_staged_plan(case):
    root, delivery = case
    other = root / "approval/plan.json"
    other.parent.mkdir()
    other.write_text('{"resource_changes": []}\n')
    approval = delivery["transitions"][5]
    approval["evidence"] = [sc.evidence_item(root, "plan", "approval/plan.json")]
    found = codes(check(delivery, root))
    assert "plan_not_staged" in found


def test_the_plan_given_must_be_the_last_plan_in_the_delivery(case):
    root, delivery = case
    staged = sc.sha256_file(root / "staging/plan.json")
    assert check(delivery, root, plan_sha256=staged) == []
    assert codes(check(delivery, root, plan_sha256="f" * 64)) == ["plan_mismatch"]


def test_an_execution_record_out_of_shape_is_a_violation(case):
    root, delivery = case
    (root / "deploy/execution.json").write_text('{"result": "succeeded"}\n')
    delivery["transitions"][6]["evidence"] = [
        sc.evidence_item(root, "execution_record", "deploy/execution.json")
    ]
    assert "evidence_content_invalid" in codes(check(delivery, root))


# Evidence files -----------------------------------------------------------------------


def _first(delivery):
    return delivery["transitions"][0]["evidence"][0]


def test_a_changed_evidence_file_is_a_hash_mismatch(case):
    root, _ = case
    delivery = sc.broken_delivery(root, "evidence_hash_mismatch")
    found = check(delivery, root)
    assert codes(found) == ["evidence_hash_mismatch"]
    assert found[0]["locator"] == "staging/test-result.json"


def test_a_missing_evidence_file_is_a_violation(case):
    root, delivery = case
    (root / "requirement/REQ-0001.json").unlink()
    assert set(codes(check(delivery, root))) == {"evidence_missing"}


@pytest.mark.parametrize("locator", ["../outside.json", "/etc/hosts", "a/../b", "./a", "a//b"])
def test_paths_that_leave_the_evidence_directory_are_refused(case, locator):
    root, delivery = case
    (root.parent / "outside.json").write_text("{}")
    _first(delivery)["locator"] = locator
    assert "evidence_path_refused" in codes(check(delivery, root))


def test_a_symlinked_evidence_file_is_not_followed(case, tmp_path):
    root, delivery = case
    target = tmp_path / "elsewhere.json"
    target.write_bytes((root / "requirement/REQ-0001.json").read_bytes())
    (root / "requirement/link.json").symlink_to(target)
    _first(delivery)["locator"] = "requirement/link.json"
    assert "evidence_symlink" in codes(check(delivery, root))


def test_a_symlinked_directory_on_the_way_is_not_followed(case, tmp_path):
    root, delivery = case
    os.symlink(root / "requirement", root / "linked")
    _first(delivery)["locator"] = "linked/REQ-0001.json"
    assert "evidence_symlink" in codes(check(delivery, root))


def test_a_directory_is_not_evidence(case):
    root, delivery = case
    _first(delivery)["locator"] = "requirement"
    assert "evidence_missing" in codes(check(delivery, root))


def test_an_evidence_file_over_the_limit_is_not_read(case, monkeypatch):
    root, delivery = case
    monkeypatch.setattr(evidence, "MAX_BYTES", 8)
    found = check(delivery, root)
    assert "evidence_too_large" in codes(found)
    assert "evidence_hash_mismatch" not in codes(found)


def test_the_reader_reads_inside_the_directory_only(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.txt").write_text("x")
    assert evidence.read(tmp_path, "a/b.txt") == (b"x", None)
    assert evidence.read(tmp_path, "a/missing.txt") == (None, "evidence_missing")
    assert evidence.read(tmp_path, "a\\b.txt") == (None, "evidence_path_refused")
    assert evidence.read(tmp_path, "") == (None, "evidence_path_refused")


# Who approved: the implementer under another spelling --------------------------------


@pytest.mark.parametrize(
    "written, key",
    [
        ("alice", "alice"),
        ("Alice", "alice"),
        ("ALICE", "alice"),
        (" alice\t", "alice"),
        ("alice@example.invalid", "alice"),
        ("Alice@Example.Invalid", "alice"),
        ("alice.", "alice"),
        ("alice..", "alice"),
        ("alice.@example.invalid", "alice"),
        ("ａｌｉｃｅ", "alice"),  # fullwidth letters fold under NFKC
        ("Straße", "strasse"),  # casefold, not lower
        ("alice.b", "alice.b"),
        ("bob", "bob"),
    ],
)
def test_a_person_is_compared_by_one_normalised_key(written, key):
    assert contract.person(written) == key


@pytest.mark.parametrize("alias", ["Alice", "ALICE", "alice@example.invalid", "alice."])
def test_the_implementer_under_another_spelling_is_still_the_implementer(case, alias):
    root, delivery = case
    index = FORWARD.index("privileged_approved")
    delivery["transitions"][index]["actor"] = {"name": alias, "role": "privileged"}
    assert codes(check(delivery, root)) == ["approver_is_implementer"]


def test_only_the_implementer_is_compared_with_the_approver(case):
    root, delivery = case
    approver = dict(delivery["transitions"][FORWARD.index("privileged_approved")]["actor"])
    for stage in ("requirement_approved", "designed", "staging_verified", "pr_opened"):
        delivery["transitions"][FORWARD.index(stage)]["actor"] = dict(approver)
    assert check(delivery, root) == []


@pytest.mark.parametrize("name", ["alice\n", "alice\r", "al\nice", "alice\x00", "alice\x1b", " "])
def test_an_actor_name_with_control_characters_is_refused_by_the_schema(case, name):
    root, delivery = case
    delivery["transitions"][2]["actor"]["name"] = name
    assert set(codes(check(delivery, root))) == {"schema"}


# A rolled-back change ------------------------------------------------------------------


def _rolled_back(root, delivery, text):
    (root / "deploy/rollback.json").write_text(text)
    delivery["transitions"].append(
        {
            "from": "deployed",
            "to": "rolled_back",
            "at": "2026-01-13T00:00:00Z",
            "actor": {"name": "bob", "role": "privileged"},
            "evidence": [sc.evidence_item(root, "execution_record", "deploy/rollback.json")],
        }
    )
    delivery["state"] = "rolled_back"
    return delivery


@pytest.mark.parametrize(
    "text", ['{"result": "rolled back"}\n', "not json\n", '["a"]\n', '{"plan_sha256": "x"}\n']
)
def test_a_rollback_execution_record_out_of_shape_is_a_violation(case, text):
    root, delivery = case
    found = check(_rolled_back(root, delivery, text), root)
    assert codes(found) == ["evidence_content_invalid"]
    assert found[0]["transition"] == 7 and found[0]["locator"] == "deploy/rollback.json"


# Evidence JSON nested too deep to parse -------------------------------------------------


def test_requirement_evidence_nested_too_deep_is_out_of_shape_not_a_crash(case):
    root, delivery = case
    (root / "requirement/REQ-0001.json").write_text("[" * 200_000 + "]" * 200_000)
    for item in delivery["transitions"]:
        for entry in item["evidence"]:
            if entry["kind"] == "requirement":
                entry["sha256"] = sc.sha256_file(root / entry["locator"])
    assert codes(check(delivery, root)) == ["evidence_content_invalid"] * 2


# Which plan the delivery delivered -----------------------------------------------------


@pytest.mark.parametrize("until", FORWARD)
def test_the_delivered_plan_is_the_last_plan_recorded(tmp_path, until):
    root = tmp_path / "evidence"
    delivery = sc.valid_delivery(root, until=until)
    staged = sc.sha256_file(root / "staging/plan.json")
    has_plan = FORWARD.index(until) >= FORWARD.index("staging_verified")
    assert contract.delivered_plan(delivery) == (staged if has_plan else None)


def test_a_delivery_out_of_schema_has_no_delivered_plan(case):
    _, delivery = case
    delivery["state"] = "applied"
    assert contract.delivered_plan(delivery) is None
