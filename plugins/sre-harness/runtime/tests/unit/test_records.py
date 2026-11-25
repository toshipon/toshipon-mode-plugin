import pytest

from sre_harness import records
from sre_harness.errors import InputError

RUN = "run-20260101T000000Z-0123abcd"


def test_ids_follow_the_improvement_harness_form():
    assert records.record_id("RT", 1) == "RT-0001"
    assert records.record_id("DEC", 12345) == "DEC-12345"
    assert records.RECORD_ID.fullmatch("EV-0001")
    for prefix, number in (("rt", 1), ("RT", 0), ("TOOLONG", 1), ("RT", True)):
        with pytest.raises(ValueError):
            records.record_id(prefix, number)


def test_fingerprints_ignore_key_order():
    assert records.fingerprint({"a": 1, "b": [1, 2]}) == records.fingerprint({"b": [1, 2], "a": 1})
    assert records.fingerprint({"a": 1}) != records.fingerprint({"a": 2})
    assert len(records.sha256("x")) == 64


def test_timestamps_are_utc_seconds():
    assert records.violations("common", records.iso(), "timestamp") == []


def test_provenance_needs_a_sha_or_a_content_hash():
    base = {
        "source_id": "S1",
        "locator": "docs/adr/0001.md#L3",
        "retrieved_at": "2026-01-01T00:00:00Z",
    }
    assert records.violations("common", {**base, "sha": "a" * 40}, "provenance") == []
    assert records.violations("common", {**base, "content_sha256": "b" * 64}, "provenance") == []
    assert records.violations("common", base, "provenance")
    with pytest.raises(InputError, match="common record is invalid") as invalid:
        records.validate("common", {**base, "sha": "short"}, "provenance")
    assert invalid.value.exit_code == 2


def test_evidence_matches_the_evaluator_contract_plus_a_quote():
    assert records.violations("common", {"id": "E1", "ref": "RT-0001"}, "evidence") == []
    assert (
        records.violations(
            "common", {"id": "E1", "ref": "RT-0001", "note": "n", "quote": "q"}, "evidence"
        )
        == []
    )
    assert records.violations("common", {"id": "E1"}, "evidence")


def test_every_record_carries_the_shared_fields():
    record = {"schema_version": 1, "id": "RT-0001", "run_id": RUN, "created_at": records.iso()}
    assert records.violations("common", record, "record") == []
    assert records.violations("common", {**record, "schema_version": 2}, "record")
    assert records.violations("common", {**record, "id": "rt-1"}, "record")


def test_jsonl_round_trips_privately(tmp_path):
    rows = [{"id": "RT-0001", "body": "日本語"}, {"id": "RT-0002"}]
    path = records.write_jsonl(tmp_path / "threads.jsonl", rows)
    assert records.read_jsonl(path) == rows
    assert path.stat().st_mode & 0o777 == 0o600
    assert records.read_json(records.write_json(tmp_path / "a.json", rows)) == rows


def test_unreadable_input_is_an_input_error(tmp_path):
    (tmp_path / "bad.jsonl").write_text("{")
    (tmp_path / "bad.json").write_text("{")
    for call in (
        lambda: records.read_jsonl(tmp_path / "bad.jsonl"),
        lambda: records.read_jsonl(tmp_path / "missing.jsonl"),
        lambda: records.read_json(tmp_path / "bad.json"),
        lambda: records.read_json(tmp_path / "missing.json"),
    ):
        with pytest.raises(InputError):
            call()


def test_jsonl_lines_end_only_at_a_newline(tmp_path):
    # json.dumps(ensure_ascii=False) leaves these characters as they are, and
    # str.splitlines() would cut a row at each of them.
    rows = [{"body": "a b c\x85d\x0ce\x1cf"}, {"body": "next"}]
    assert records.read_jsonl(records.write_jsonl(tmp_path / "rows.jsonl", rows)) == rows
