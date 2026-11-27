"""examples/synthetic/container_probe.py without Docker: the parts that judge a measurement,
and the exit code when docker is not there. The measurement itself needs Docker and is not run
here (README「コンテナの経路」の実測)."""

import json
import os

import pytest

from examples.synthetic import container_probe as probe
from examples.synthetic import knowledge_corpus as kc


def test_spread_and_the_cold_call_apart():
    assert probe.spread([]) is None
    assert probe.series([0.5, 0.2, 0.1, 0.3]) == {
        "cold": 0.5,
        "warm": {"n": 3, "min": 0.1, "median": 0.2, "max": 0.3},
    }


@pytest.mark.parametrize(
    ("first", "second", "kind"),
    [
        ({"code": 0, "locked": False}, {"code": 1, "locked": True}, "overlapped"),
        ({"code": 1, "locked": True}, {"code": 0, "locked": False}, "overlapped"),
        ({"code": 0, "locked": False}, {"code": 0, "locked": False}, "serial"),
        # Exit 1 for another reason is no proof of the lock.
        ({"code": 0, "locked": False}, {"code": 1, "locked": False}, "anomaly"),
        ({"code": 1, "locked": True}, {"code": 1, "locked": True}, "anomaly"),
        ({"code": 0, "locked": False}, {"code": 2, "locked": False}, "anomaly"),
    ],
)
def test_a_pair_of_updates_is_overlapped_only_when_one_stopped_on_the_lock(first, second, kind):
    assert probe.classify_pair(first, second) == kind


def search(generation, summaries, truncated=0):
    return {
        "snapshot": {"generation": generation},
        "records": [{"id": f"KN-{n:012x}", "summary": text} for n, text in enumerate(summaries)],
        "proposed": [],
        "truncated": {"count": truncated},
    }


def test_a_reader_answer_is_compared_with_the_reference_of_its_generation():
    references = {
        3: probe.answer_of(search(3, ["a [s1]"])),
        4: probe.answer_of(search(4, ["a [s2]"])),
    }
    answers = [
        probe.answer_of(search(3, ["a [s1]"])),
        probe.answer_of(search(4, ["a [s2]"])),
        probe.answer_of(search(4, ["a [s1]"])),  # generation 4 with the text of 3
        probe.answer_of(search(5, ["a [s3]"])),  # a generation nobody recorded
    ]
    assert probe.mismatches(answers, references) == [4, 5]


def test_every_variant_changes_every_summary_with_room():
    corpus = kc.scenario(50).corpus
    changed = probe.variant(corpus, "s1")
    assert changed is not corpus
    for before, after in zip(corpus["items"], changed["items"]):
        if len(before["summary"]) + 5 <= 240:
            assert after["summary"] == before["summary"] + " [s1]"
    assert corpus["items"][0]["summary"] != changed["items"][0]["summary"]


def test_the_base_is_read_from_the_dockerfile():
    assert "@sha256:" in probe.base_digest(probe.DOCKERFILE.read_text())


def test_the_probe_uses_the_wrappers_hardening_options():
    text = probe.WRAPPER.read_text()
    for value in probe.HARDENING:
        if value.startswith("/tmp:"):
            assert '"/tmp:rw,noexec,nosuid,size=$TMPFS_SIZE"' in text
        elif value not in {"128", "512m"}:
            assert value in text
    assert "PIDS_LIMIT=128" in text and "DEFAULT_MEMORY=512m" in text


def test_without_docker_it_stops_with_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert probe.main(["--out", str(tmp_path / "out")]) == 2
    assert "docker" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_out_is_required_except_for_the_smoke_check():
    with pytest.raises(SystemExit):
        probe.parse([])
    assert probe.parse(["--smoke"]).pairs == 2


def test_the_smoke_verdict_names_what_failed():
    good = {
        "basic": {"status_fts5": True, "update": True},
        "exclusive": {
            "held": {"n": 1, "refused_with_exit_1": 1, "generation_unchanged": 1},
            "concurrent": {"anomaly": 0, "generation_matches": True},
        },
        "snapshot": {"mismatches": 0, "reader_failures": 0, "write_failures": 0},
    }
    assert probe.failed(good) == []
    bad = json.loads(json.dumps(good))
    bad["basic"]["update"] = False
    bad["exclusive"]["held"]["refused_with_exit_1"] = 0
    bad["snapshot"]["mismatches"] = 1
    assert probe.failed(bad) == ["update", "held lock: an update was not refused", "snapshot"]


def test_the_probe_names_what_it_makes_for_removal():
    assert probe.PREFIX == "sre-harness-test-"
    assert probe.Volume("image", os.getcwd()).name.startswith(probe.PREFIX)
