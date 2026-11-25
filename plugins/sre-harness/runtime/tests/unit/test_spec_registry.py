"""Decision Registry: supersession, the active set, cycles and the invariants `check` enforces."""

import pytest

from sre_harness import records
from sre_harness.errors import InputError
from sre_harness.spec import registry
from tests.spec_helpers import decision


def built(*drafts):
    result = registry.build(list(drafts))
    assert registry.check(result["decisions"]) == []
    for item in result["decisions"]:
        assert records.violations("decision", item) == [], item["id"]
    return result, {item["id"]: item for item in result["decisions"]}


def test_an_approved_successor_supersedes_the_old_decision():
    result, by_id = built(decision(2), decision(5, supersedes=["ADR-0002"]))
    assert by_id["DEC-0002"]["status"] == "superseded"
    assert by_id["DEC-0002"]["superseded_by"] == ["DEC-0005"]
    assert by_id["DEC-0005"]["supersedes"] == ["DEC-0002"]
    assert by_id["DEC-0005"]["status"] == "approved"
    assert result["active"] == ["DEC-0005"]


def test_a_document_cannot_name_an_id_the_harness_assigned():
    result, by_id = built(decision(2), decision(5, supersedes=["DEC-0002"]))
    assert by_id["DEC-0002"]["status"] == "approved"
    assert result["input_missing"] == [
        {"kind": "decision_ref", "from": "DEC-0005", "ref": "DEC-0002"}
    ]


def test_a_written_id_is_never_taken_for_another_decision_s_registry_id():
    written = decision(1, external_id="DEC-0002")  # registry DEC-0001, the ADR says DEC-0002
    other = decision(2, external_id="ADR-0005")  # registry DEC-0002
    successor = decision(3, external_id="ADR-0006", supersedes=["DEC-0002"])
    _, by_id = built(written, other, successor)
    assert by_id["DEC-0001"]["status"] == "superseded"
    assert by_id["DEC-0002"]["status"] == "approved"
    assert by_id["DEC-0003"]["supersedes"] == ["DEC-0001"]


def test_two_decisions_with_one_written_id_are_refused_with_exit_2():
    with pytest.raises(InputError) as error:
        registry.build([decision(1), decision(2, external_id="ADR-0001")])
    assert error.value.exit_code == 2 and "ADR-0001" in str(error.value)


def test_an_inferred_draft_names_registry_ids_and_reaches_explicit_decisions_only():
    proposal = decision(
        1,
        status="proposed",
        basis="inferred",
        id="DCI-0001",
        external_id=None,
        supersedes=["DEC-0002", "DCI-0001"],
    )
    del proposal["external_id"]
    result, by_id = built(decision(2), proposal)
    assert by_id["DCI-0001"]["supersedes"] == ["DEC-0002"]
    assert by_id["DEC-0002"]["status"] == "approved"
    assert {"kind": "decision_ref", "from": "DCI-0001", "ref": "DCI-0001"} in result[
        "input_missing"
    ]


def test_in_a_chain_of_three_only_the_last_is_active():
    result, by_id = built(
        decision(3, status="superseded"),
        decision(4, status="superseded", supersedes=["ADR-0003"]),
        decision(6, supersedes=["ADR-0004"]),
    )
    assert [by_id[f"DEC-000{n}"]["status"] for n in (3, 4, 6)] == [
        "superseded",
        "superseded",
        "approved",
    ]
    assert by_id["DEC-0003"]["superseded_by"] == ["DEC-0004"]
    assert by_id["DEC-0004"]["superseded_by"] == ["DEC-0006"]
    assert result["active"] == ["DEC-0006"]


def test_a_rejected_decision_cannot_supersede():
    result, by_id = built(decision(5), decision(7, status="rejected", supersedes=["ADR-0005"]))
    assert by_id["DEC-0005"]["status"] == "approved"
    assert by_id["DEC-0005"]["superseded_by"] == []
    assert by_id["DEC-0007"]["status"] == "rejected"
    assert any(issue["kind"] == "cannot_supersede" for issue in by_id["DEC-0007"]["issues"])
    assert result["active"] == ["DEC-0005"]


def test_a_proposed_decision_does_not_supersede_yet():
    _, by_id = built(decision(2), decision(5, status="proposed", supersedes=["ADR-0002"]))
    assert by_id["DEC-0002"]["status"] == "approved"
    assert by_id["DEC-0002"]["superseded_by"] == []


def test_stated_superseded_without_an_approved_successor_becomes_unresolved():
    _, by_id = built(decision(2, status="superseded"))
    assert by_id["DEC-0002"]["status"] == "unresolved"
    assert by_id["DEC-0002"]["stated_status"] == "superseded"
    assert by_id["DEC-0002"]["issues"][0]["kind"] == "no_approved_successor"


def test_approved_without_an_approver_is_not_approved():
    result, by_id = built(decision(9, approvers=[]))
    assert by_id["DEC-0009"]["status"] == "unresolved"
    assert by_id["DEC-0009"]["issues"][0]["kind"] == "approval_incomplete"
    assert result["active"] == []


def test_approved_without_evidence_is_not_approved():
    _, by_id = built(decision(9, evidence=[]))
    assert by_id["DEC-0009"]["status"] == "unresolved"


def test_an_unapproved_successor_does_not_count_even_when_it_says_approved():
    _, by_id = built(decision(2), decision(5, approvers=[], supersedes=["ADR-0002"]))
    assert by_id["DEC-0002"]["status"] == "approved"
    assert by_id["DEC-0005"]["status"] == "unresolved"


def test_a_cycle_is_refused_with_exit_2():
    with pytest.raises(InputError) as error:
        registry.build(
            [decision(11, supersedes=["ADR-0012"]), decision(12, supersedes=["ADR-0011"])]
        )
    assert error.value.exit_code == 2
    assert "cycle" in str(error.value)
    assert "DEC-0011" in str(error.value) and "DEC-0012" in str(error.value)


def test_a_self_reference_is_a_cycle():
    with pytest.raises(InputError):
        registry.build([decision(1, supersedes=["ADR-0001"])])


def test_a_longer_cycle_is_found():
    with pytest.raises(InputError):
        registry.build(
            [
                decision(1, supersedes=["ADR-0003"]),
                decision(2, supersedes=["ADR-0001"]),
                decision(3, supersedes=["ADR-0002"]),
            ]
        )


def test_a_reference_to_a_missing_decision_is_input_missing():
    result, by_id = built(decision(4, supersedes=["ADR-0099"]))
    assert by_id["DEC-0004"]["supersedes"] == []
    assert result["input_missing"] == [
        {"kind": "decision_ref", "from": "DEC-0004", "ref": "ADR-0099"}
    ]


# check(): the invariants, on registries the builder would never produce -----------------


def finished(*items):
    """Decisions as stored: superseded_by and issues present."""
    return [
        {"superseded_by": [], "issues": [], "stated_status": item["status"], **item}
        for item in items
    ]


def test_check_refuses_superseded_by_a_decision_that_is_not_approved():
    old = decision(2, status="superseded", superseded_by=["DEC-0005"])
    new = decision(5, status="proposed", supersedes=["DEC-0002"])
    problems = registry.check(finished(old, new))
    assert any("DEC-0002" in problem and "approved" in problem for problem in problems)


def test_check_refuses_superseded_without_superseded_by():
    assert registry.check(finished(decision(2, status="superseded")))


def test_check_refuses_approved_without_approvers_or_evidence():
    assert registry.check(finished(decision(1, approvers=[])))
    assert registry.check(finished(decision(1, evidence=[])))


def test_check_refuses_an_inferred_approved_decision():
    problems = registry.check(finished(decision(1, basis="inferred")))
    assert any("inferred" in problem for problem in problems)


def test_check_refuses_a_rejected_decision_in_superseded_by():
    old = decision(5, status="superseded", superseded_by=["DEC-0007"])
    new = decision(7, status="rejected", supersedes=["DEC-0005"])
    assert registry.check(finished(old, new))


def test_check_refuses_a_cycle_and_a_dangling_id():
    one = decision(1, supersedes=["DEC-0002"])
    two = decision(2, supersedes=["DEC-0001"])
    assert any("cycle" in problem for problem in registry.check(finished(one, two)))
    assert registry.check(finished(decision(1, supersedes=["DEC-0042"])))


def test_check_accepts_what_build_produces():
    result = registry.build(
        [
            decision(3, status="superseded"),
            decision(4, status="superseded", supersedes=["ADR-0003"]),
            decision(6, supersedes=["ADR-0004"]),
            decision(7, status="rejected", supersedes=["ADR-0006"]),
        ]
    )
    assert registry.check(result["decisions"]) == []
    assert registry.active(result["decisions"]) == ["DEC-0006"]
