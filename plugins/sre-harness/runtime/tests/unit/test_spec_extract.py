"""`spec extract`: explicit entries by fixed rules first, inferred ones checked against quotes."""

import pytest

from examples.synthetic.spec_scenarios import (
    HTTP_CLIENT_NEW,
    HTTP_CLIENT_OLD,
    INJECTED_ADR,
    INJECTION,
    LEADS,
    MINUTES_DECISION,
    MINUTES_REQUIREMENT,
    MISSING_ADR,
    NO_APPROVER_ADR,
    REJECTED_ADR,
    RETENTION_CHAIN,
    RETENTION_DAYS,
    RETENTION_REQ,
    _adr,
    _write,
    write_cycle_workspace,
    write_workspace,
)
from sre_harness import llm, records
from sre_harness.spec import extract as extract_stage
from tests.helpers import LLM_ARGS, prompt_parts, read_jsonl, run_dir, with_home
from tests.spec_helpers import by_external, documents, extract, ingested, read


@pytest.fixture
def run_id(tmp_path, home):
    return ingested(home, write_workspace(tmp_path / "ws"))


def extracted(home, run_id, *extra, backend="stub"):
    code, summary = extract(home, run_id, *extra, backend=backend)
    assert code == 0, summary
    return summary, read(home, run_id, "decisions.json"), read(home, run_id, "requirements.json")


def requirement_by_external(registry):
    return {item.get("external_id"): item for item in registry["requirements"]}


def test_explicit_decisions_and_their_supersession(home, run_id):
    _, decisions, _ = extracted(home, run_id)
    adr = by_external(decisions)
    old, new = adr[HTTP_CLIENT_OLD], adr[HTTP_CLIENT_NEW]
    assert old["status"] == "superseded" and old["superseded_by"] == [new["id"]]
    assert new["status"] == "approved" and new["supersedes"] == [old["id"]]
    first, second, last = (adr[name] for name in RETENTION_CHAIN)
    assert [first["status"], second["status"], last["status"]] == [
        "superseded",
        "superseded",
        "approved",
    ]
    assert adr[REJECTED_ADR]["status"] == "rejected"
    assert new["superseded_by"] == []
    assert adr[INJECTED_ADR]["status"] == "proposed"
    assert adr[NO_APPROVER_ADR]["status"] == "unresolved"
    assert adr[NO_APPROVER_ADR]["stated_status"] == "approved"
    assert adr[NO_APPROVER_ADR]["decided_at"] == "2026-01-12"
    active = {adr[name]["id"] for name in ("ADR-0001", HTTP_CLIENT_NEW, RETENTION_CHAIN[-1])}
    assert set(decisions["active"]) == active
    for item in decisions["decisions"]:
        assert records.violations("decision", item) == [], item["id"]
    assert records.violations("decision", decisions, "registry") == []


def test_explicit_decisions_carry_their_source_fields(home, run_id):
    _, decisions, _ = extracted(home, run_id)
    new = by_external(decisions)[HTTP_CLIENT_NEW]
    assert new["basis"] == "explicit" and new["confidence"] == "high"
    assert new["approvers"] == [
        {"name": "alice", "role": "tech lead"},
        {"name": "bob", "role": "sre"},
    ]
    assert new["scope"] == {
        "systems": ["log-pipeline"],
        "components": [],
        "environments": ["staging", "production"],
    }
    assert new["title"] == "HTTP クライアントを B に移行する"
    assert new["decided_at"] == "2026-01-10"


def test_every_quote_is_a_substring_of_its_section(home, run_id):
    _, decisions, requirements = extracted(home, run_id)
    sections = {
        f"{doc['id']}#{section['id']}": section["text"]
        for doc in documents(home, run_id)
        for section in doc["sections"]
    }
    for item in decisions["decisions"] + requirements["requirements"]:
        assert item["evidence"], item["id"]
        for evidence in item["evidence"]:
            assert evidence["quote"] in sections[evidence["ref"]]


def test_explicit_requirements_and_acceptance_criteria(home, run_id):
    _, decisions, requirements = extracted(home, run_id)
    adr = by_external(decisions)
    req = requirement_by_external(requirements)
    retention = req[RETENTION_REQ]
    assert retention["id"] == RETENTION_REQ and retention["basis"] == "explicit"
    assert (
        retention["statement"]
        == f"アプリケーションログを {RETENTION_DAYS} 日保持しなければならない。"
    )
    assert retention["nfr_category"] == "compliance"
    assert retention["decision_refs"] == [adr["ADR-0006"]["id"]]
    (criterion,) = retention["acceptance_criteria"]
    assert criterion["given"] == "保持期間の設定が適用されている"
    assert criterion["when"] == "日次の削除ジョブが動く"
    assert criterion["measurable"] is True
    assert (criterion["target"], criterion["unit"]) == (RETENTION_DAYS, "日")
    assert retention["rationale"] == "監査で 1 年分の記録を求められるため。"
    assert retention["ambiguity"] == []

    timeout = req["REQ-0004"]
    assert timeout["decision_refs"] == [adr[HTTP_CLIENT_OLD]["id"]]
    assert timeout["acceptance_criteria"][0]["then"] == "the call fails within 10 s"
    assert req["REQ-0005"]["nfr_category"] == "operability"
    for item in requirements["requirements"]:
        assert records.violations("requirement", item) == [], item["id"]
    assert records.violations("requirement", requirements, "registry") == []


def test_ambiguity_is_found_by_rule(home, run_id):
    summary, _, requirements = extracted(home, run_id)
    req = requirement_by_external(requirements)
    vague = {(a["kind"], a["detector"]) for a in req["REQ-0002"]["ambiguity"]}
    assert vague == {("vague_term", "rule"), ("no_metric", "rule"), ("no_criteria", "rule")}
    assert ("compound", "rule") in {
        (a["kind"], a["detector"]) for a in req["REQ-0003"]["ambiguity"]
    }
    assert summary["ambiguity"]["rule"] >= 4


def test_a_reference_to_a_missing_decision_is_input_missing(home, run_id):
    summary, decisions, requirements = extracted(home, run_id)
    req = requirement_by_external(requirements)
    assert req["REQ-0003"]["decision_refs"] == []
    entry = {"kind": "decision_ref", "from": "REQ-0003", "ref": MISSING_ADR}
    assert entry in requirements["input_missing"]
    info = read(home, run_id, "extract.json")
    assert entry in info["input_missing"]
    assert {
        "source_id": "file:docs/absent.md",
        "locator": "docs/absent.md",
        "reason": "file_not_found",
    } in info["input_missing"]
    assert summary["input_missing"] == len(info["input_missing"])


def test_the_stub_infers_from_minutes_and_marks_it_inferred(home, run_id):
    summary, decisions, requirements = extracted(home, run_id)
    inferred = [d for d in decisions["decisions"] if d["basis"] == "inferred"]
    assert [d["content"] for d in inferred] == [MINUTES_DECISION]
    assert inferred[0]["status"] == "proposed" and inferred[0]["confidence"] == "low"
    assert "external_id" not in inferred[0]
    req = [r for r in requirements["requirements"] if r["basis"] == "inferred"]
    assert [r["statement"] for r in req] == [MINUTES_REQUIREMENT]
    assert req[0]["id"] == "RQI-0001"  # its own namespace, never a REQ- id a document writes
    assert inferred[0]["id"] == "DCI-0001"
    llm_flags = [
        a for r in requirements["requirements"] for a in r["ambiguity"] if a["detector"] == "llm"
    ]
    assert [(a["kind"], a["span"]) for a in llm_flags] == [("vague_term", "TBD")]
    assert summary["ambiguity"]["llm"] == 1
    calls = read_jsonl(run_dir(home, run_id) / llm.CALLS_FILE)
    assert [row["stage"] for row in calls] == ["extract"]


def test_a_cycle_stops_extract_with_exit_2(tmp_path, home, capsys):
    run_id = ingested(home, write_cycle_workspace(tmp_path / "cycle"))
    code, _ = extract(home, run_id)
    assert code == 2
    error = capsys.readouterr().err
    assert "docs/adr/0011-a.md" in error and "docs/adr/0012-b.md" in error
    assert not (run_dir(home, run_id) / "decisions.json").exists()


def test_extract_needs_an_ingested_run(home, tmp_path):
    from sre_harness import paths

    run = paths.create_run(paths.state_root(home))
    assert extract(home, run.name)[0] == 2


# A model's answer is held to quotes, ids and the invariants --------------------------------


def section_ref(data, needle):
    for doc in data["documents"]:
        for section in doc["sections"]:
            if needle in section["text"]:
                return section["ref"]
    raise AssertionError(needle)


def model_answer(data):
    minutes = section_ref(data, MINUTES_DECISION)
    wanted = section_ref(data, MINUTES_REQUIREMENT)
    return {
        "decisions": [
            {
                "key": "D1",
                "title": "ダッシュボード",
                "content": MINUTES_DECISION,
                "rationale": "",
                "status": "approved",  # the document asked for it; it must not stick
                "supersedes": [],
                "scope": {"systems": [], "components": [], "environments": []},
                "evidence": [{"ref": minutes, "quote": MINUTES_DECISION}],
            },
            {
                "key": "D2",
                "title": "週次レビュー",
                "content": MINUTES_DECISION,
                "rationale": "",
                "status": "proposed",
                "supersedes": [MISSING_ADR],
                "scope": {"systems": [], "components": [], "environments": []},
                "evidence": [{"ref": minutes, "quote": MINUTES_DECISION}],
            },
        ],
        "requirements": [
            {
                "key": "R1",
                "statement": MINUTES_REQUIREMENT,
                "rationale": "",
                "nfr_category": "operability",
                "decision_refs": [],
                "acceptance_criteria": [],
                "evidence": [{"ref": wanted, "quote": MINUTES_REQUIREMENT}],
            },
            {
                "key": "R2",
                "statement": "存在しない要求",
                "rationale": "",
                "nfr_category": None,
                "decision_refs": [],
                "acceptance_criteria": [],
                "evidence": [{"ref": wanted, "quote": "この文は原文にない"}],
            },
            {
                "key": "R3",
                "statement": "別の文書",
                "rationale": "",
                "nfr_category": None,
                "decision_refs": [],
                "acceptance_criteria": [],
                "evidence": [{"ref": "DOC-9999#S1", "quote": "どこにもない文書の引用"}],
            },
        ],
        "ambiguity": [
            {"requirement": "REQ-0002", "kind": "unbounded", "span": "API は高速に応答すること。"},
            {"requirement": "REQ-0002", "kind": "vague_term", "span": "not in the text"},
            {"requirement": "REQ-0042", "kind": "vague_term", "span": "x"},
            {"requirement": "R1", "kind": "unbounded", "span": "5 分以内"},
        ],
    }


def test_a_model_answer_is_checked_before_it_is_kept(tmp_path, home, run_id, fake_claude):
    env = with_home(home, tmp_path)
    seen = fake_claude(model_answer)
    code, summary = extract(env, run_id, *LLM_ARGS, backend="claude")
    assert code == 0, summary
    assert len(seen) == 1
    decisions = read(home, run_id, "decisions.json")
    requirements = read(home, run_id, "requirements.json")
    inferred = [d for d in decisions["decisions"] if d["basis"] == "inferred"]
    assert [d["status"] for d in inferred] == ["proposed"]  # D2 kept, D1 refused
    assert {"kind": "decision_ref", "from": inferred[0]["id"], "ref": MISSING_ADR} in decisions[
        "input_missing"
    ]
    adr = by_external(decisions)
    assert adr[INJECTED_ADR]["status"] == "proposed"

    rejected = read_jsonl(run_dir(home, run_id) / "rejected-extractions.jsonl")
    reasons = {row["key"]: row["reasons"][0].split(":")[0] for row in rejected}
    assert reasons == {
        "D1": "inferred_cannot_be_approved",
        "R2": "quote_not_found",
        "R3": "unknown_section",
        "ambiguity[1]": "span_not_found",
        "ambiguity[2]": "unknown_requirement",
    }
    req = {r["statement"]: r for r in requirements["requirements"]}
    kept = req[MINUTES_REQUIREMENT]
    assert kept["basis"] == "inferred"
    assert {(a["kind"], a["detector"]) for a in kept["ambiguity"]} >= {
        ("unbounded", "llm"),
        ("no_criteria", "rule"),
    }
    added = [a for a in req["API は高速に応答すること。"]["ambiguity"] if a["detector"] == "llm"]
    assert [(a["kind"], a["span"]) for a in added] == [("unbounded", "API は高速に応答すること。")]
    assert summary["ambiguity"]["llm"] == 2
    assert summary["rejected"] == 5


def test_injected_text_travels_as_data(tmp_path, home, run_id, fake_claude):
    seen = fake_claude(lambda data: {"decisions": [], "requirements": [], "ambiguity": []})
    code, _ = extract(with_home(home, tmp_path), run_id, *LLM_ARGS, backend="claude")
    assert code == 0
    instructions, data = prompt_parts(seen[0].prompt)
    assert INJECTION not in instructions
    texts = [s["text"] for doc in data["documents"] for s in doc["sections"]]
    assert any(INJECTION in text for text in texts)
    assert by_external(read(home, run_id, "decisions.json"))[INJECTED_ADR]["status"] == "proposed"


def test_a_deny_term_stops_before_sending(tmp_path, home, run_id, fake_claude):
    seen = fake_claude(lambda data: {"decisions": [], "requirements": [], "ambiguity": []})
    terms = tmp_path / "deny.txt"
    terms.write_text("log-pipeline\n")
    argv = [*LLM_ARGS, "--deny-terms-file", str(terms)]
    code, _ = extract(with_home(home, tmp_path), run_id, *argv, backend="claude")
    assert code == 3 and seen == []


def test_claude_needs_auth_mode_and_budget(home, run_id):
    assert extract(home, run_id, backend="claude")[0] == 2


def test_a_document_too_large_for_one_batch_stops(home, run_id, monkeypatch):
    monkeypatch.setattr(extract_stage, "MAX_BATCH_BYTES", 100)
    code, _ = extract(home, run_id)
    assert code == 1


# Ids: what documents write, what the harness assigns, what the backend proposes ----------


def small_workspace(directory, files):
    listed = [(path, "minutes" if "minutes" in path else "adr") for path in files]
    listed = [(p, "markdown" if p.endswith("requirements.md") else k) for p, k in listed]
    return _write(directory, files, listed)


MINUTES_ONLY = "# 定例\n\n## 議事\n\n決定: 夜間バッチの結果は毎朝確認する。\n\n要求: 夜間バッチは監視されること。\n"


def test_a_document_reference_never_resolves_to_an_inferred_entry(tmp_path, home):
    files = {
        "docs/adr/0001-a.md": _adr(1, "ログは JSON で出す", "approved", approvers=LEADS),
        "docs/minutes/2026-01-10.md": MINUTES_ONLY,
        # DEC-0002 is what the inferred decision used to get (one explicit decision + 1)
        "docs/requirements.md": "# R\n\n## REQ-0001: 監視\n\n監視すること。\n\nDecisions: DEC-0002\n",
    }
    run_id = ingested(home, small_workspace(tmp_path / "ws", files))
    _, decisions, requirements = extracted(home, run_id)
    assert [d["id"] for d in decisions["decisions"]] == ["DEC-0001", "DCI-0001"]
    assert [r["id"] for r in requirements["requirements"]] == ["REQ-0001", "RQI-0001"]
    req = requirement_by_external(requirements)["REQ-0001"]
    assert req["decision_refs"] == []
    assert {"kind": "decision_ref", "from": "REQ-0001", "ref": "DEC-0002"} in requirements[
        "input_missing"
    ]


def test_an_adr_that_writes_a_dec_id_keeps_it_as_its_registry_id(tmp_path, home):
    written = _adr(1, "キューは X を使う", "approved", approvers=LEADS)
    files = {
        "docs/adr/a.md": written.replace("id: ADR-0001", "id: DEC-0002").replace(
            "# ADR-0001: ", "# "
        ),
        "docs/adr/b.md": _adr(5, "キューの監視", "approved", approvers=LEADS),
        "docs/adr/c.md": _adr(
            6, "キューは Y を使う", "approved", supersedes=("DEC-0002",), approvers=LEADS
        ),
    }
    run_id = ingested(home, small_workspace(tmp_path / "ws", files))
    _, decisions, _ = extracted(home, run_id)
    adr = by_external(decisions)
    assert adr["DEC-0002"]["id"] == "DEC-0002"
    assert adr["DEC-0002"]["status"] == "superseded"
    assert adr["DEC-0002"]["superseded_by"] == [adr["ADR-0006"]["id"]]
    assert adr["ADR-0005"]["status"] == "approved" and adr["ADR-0005"]["id"] != "DEC-0002"
    assert len({d["id"] for d in decisions["decisions"]}) == 3


def test_two_documents_with_one_written_id_stop_extract_with_exit_2(tmp_path, home, capsys):
    files = {
        "docs/adr/a.md": _adr(1, "キューは X を使う", "approved", approvers=LEADS),
        "docs/adr/b.md": _adr(1, "キューは Y を使う", "approved", approvers=LEADS),
    }
    run_id = ingested(home, small_workspace(tmp_path / "ws", files))
    code, _ = extract(home, run_id)
    assert code == 2
    assert "ADR-0001" in capsys.readouterr().err
    assert not (run_dir(home, run_id) / "decisions.json").exists()


def answer_superseding(targets, quote=MINUTES_DECISION, needle=MINUTES_DECISION):
    def answer(data):
        ref = section_ref(data, needle)
        return {
            "decisions": [
                {
                    "key": "D1",
                    "title": "x",
                    "content": "x",
                    "rationale": "",
                    "status": "proposed",
                    "supersedes": list(targets),
                    "scope": {"systems": [], "components": [], "environments": []},
                    "evidence": [{"ref": ref, "quote": quote}],
                }
            ],
            "requirements": [],
            "ambiguity": [],
        }

    return answer


def test_an_inferred_decision_keeps_only_supersedes_that_name_explicit_decisions(
    tmp_path, home, run_id, fake_claude
):
    explicit = by_external(extracted(home, run_id)[1])
    target = explicit["ADR-0006"]["id"]
    wanted = ["DCI-0001", "DCI-0002", "DEC-0010", "DEC-0099", target, "ADR-0005"]
    fake_claude(answer_superseding(wanted))
    code, summary = extract(with_home(home, tmp_path), run_id, *LLM_ARGS, backend="claude")
    assert code == 0, summary
    decisions = read(home, run_id, "decisions.json")
    (inferred,) = [d for d in decisions["decisions"] if d["basis"] == "inferred"]
    assert inferred["supersedes"] == [target, explicit["ADR-0005"]["id"]]
    dropped = [i["detail"] for i in inferred["issues"] if i["kind"] == "supersedes_dropped"]
    assert len(dropped) == 4
    assert all(any(ref in text for text in dropped) for ref in wanted[:4])
    assert not any(m.get("from") == inferred["id"] for m in decisions["input_missing"])
    assert explicit["ADR-0006"]["status"] == by_external(decisions)["ADR-0006"]["status"]


def test_a_quote_shorter_than_eight_characters_is_not_evidence(tmp_path, home, run_id, fake_claude):
    quote = "トは 5 分以内"  # in the section, 8 characters, 6 without the spaces
    assert quote in MINUTES_REQUIREMENT
    fake_claude(answer_superseding([], quote=quote, needle=MINUTES_REQUIREMENT))
    code, summary = extract(with_home(home, tmp_path), run_id, *LLM_ARGS, backend="claude")
    assert code == 0, summary
    kept = read(home, run_id, "decisions.json")["decisions"]
    assert not [d for d in kept if d["basis"] == "inferred"]
    rejected = read_jsonl(run_dir(home, run_id) / "rejected-extractions.jsonl")
    assert [row["reasons"][0].split(":")[0] for row in rejected] == ["quote_too_short"]


def test_the_extract_schema_wants_quotes_of_eight_characters():
    answer = answer_superseding([], quote="監視")(
        {"documents": [{"sections": [{"ref": "DOC-0001#S1", "text": MINUTES_DECISION}]}]}
    )
    assert records.violations("llm-spec-extract", answer) != []
