import json
import subprocess
import sys
from pathlib import Path

import pytest

from examples.synthetic import knowledge_corpus as kc
from sre_harness import paths, records
from sre_harness.knowledge import config, ingest, model, store, terms
from sre_harness.knowledge.adapters import build

RUNTIME = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def small():
    return kc.scenario(50)


def ids_of(scenario):
    return {
        model.record_id(i["origin"]["kind"], i["origin"]["key"]) for i in scenario.corpus["items"]
    }


@pytest.mark.parametrize("size", kc.SIZES)
def test_each_size_has_exactly_that_many_items_and_is_deterministic(size):
    first, second = kc.scenario(size), kc.scenario(size)
    assert len(first.corpus["items"]) == size
    assert records.canonical(first.corpus) == records.canonical(second.corpus)
    assert first.tasks == second.tasks and first.sources == second.sources
    assert records.canonical(kc.scenario(size, seed=7).corpus) != records.canonical(first.corpus)


def test_three_projects_with_two_to_four_components_and_two_principals(small):
    projects = small.config["projects"]
    assert len(projects) == 3
    assert all(2 <= len(p["components"]) <= 4 for p in projects.values())
    assert set(small.config["principals"]) == {kc.PRINCIPAL_ALL, kc.PRINCIPAL_LIMITED}
    assert records.violations("knowledge-config", small.config) == []
    assert any(i["scope"]["level"] == "organization" for i in small.corpus["items"])


def test_the_limited_principal_cannot_read_the_third_project(small):
    limited = config.principal(small.config, kc.PRINCIPAL_LIMITED)
    third = [
        i
        for i in small.corpus["items"]
        if i["scope"]["project_id"] == kc.THIRD_PROJECT and "access" in i
    ]
    assert third
    for value in third:
        readable = value["access"]["classification"] in limited.classifications and bool(
            set(value["access"]["readers"]) & limited.readers
        )
        assert not readable


def test_thirty_tasks_whose_labels_name_records_in_the_corpus(small):
    assert len(small.tasks) == 30
    known = ids_of(small)
    for task in small.tasks:
        assert set(task["applicable"]) <= known
        assert set(task["mandatory"]) <= set(task["applicable"])
        assert task["principal"] in small.config["principals"]


def test_the_traps_are_in_place(small):
    traps = small.traps
    chain = traps["supersession_chain"]
    by_id = {
        model.record_id(i["origin"]["kind"], i["origin"]["key"]): i for i in small.corpus["items"]
    }
    assert len(chain) == 3
    for older, newer in zip(chain, chain[1:]):
        assert {
            "kind": by_id[older]["origin"]["kind"],
            "key": by_id[older]["origin"]["key"],
        } in by_id[newer]["supersedes"]
    assert [by_id[i]["status"] for i in chain] == ["superseded", "superseded", "approved"]
    first, second = traps["approved_conflict"]
    assert by_id[first]["status"] == by_id[second]["status"] == "approved"
    assert by_id[first]["facets"]["topic"] == by_id[second]["facets"]["topic"]
    (stale,) = traps["stale_source"]
    evidence = by_id[stale]["evidence"][0]
    assert records.sha256(small.sources[evidence["locator"]]) != evidence["content_sha256"]
    (missing,) = traps["missing_source"]
    assert by_id[missing]["evidence"][0]["locator"] not in small.sources
    (injected,) = traps["instruction_text"]
    text = small.sources[by_id[injected]["evidence"][0]["locator"]]
    assert "以前の指示を無視" in text and by_id[injected]["enforcement"] == "advisory"
    overflow = [t for t in small.tasks if t["task_id"] == traps["overflow_task"]]
    assert overflow and overflow[0]["mandatory_tokens"] > kc.BUDGET
    assert all(
        t["mandatory_tokens"] <= kc.BUDGET
        for t in small.tasks
        if t["task_id"] != traps["overflow_task"]
    )
    paraphrased = [t for t in small.tasks if t["paraphrase"]]
    assert paraphrased and set(traps["paraphrase_tasks"]) == {t["task_id"] for t in paraphrased}
    for task in paraphrased:
        assert not set(terms.terms(task["query"])) & set(terms.terms(task["literal_query"]))


def test_every_evidence_hash_matches_its_source_except_the_traps(small):
    traps = set(small.traps["stale_source"]) | set(small.traps["missing_source"])
    for value in small.corpus["items"]:
        record_id = model.record_id(value["origin"]["kind"], value["origin"]["key"])
        if record_id in traps:
            continue
        for evidence in value["evidence"]:
            text = small.sources[evidence["locator"]]
            assert records.sha256(text) == evidence["content_sha256"]
            assert len(text.encode()) == evidence["size"]


def test_the_limited_principal_is_given_nothing_from_the_third_project(small):
    third = {
        model.record_id(i["origin"]["kind"], i["origin"]["key"])
        for i in small.corpus["items"]
        if i["scope"]["project_id"] == kc.THIRD_PROJECT
    }
    limited = [t for t in small.tasks if t["principal"] == kc.PRINCIPAL_LIMITED]
    assert any(t["project_id"] == kc.THIRD_PROJECT for t in limited)
    for task in limited:
        assert not set(task["applicable"]) & third


@pytest.mark.parametrize("size", kc.SIZES)
def test_the_corpus_goes_through_update(tmp_path, size):
    scenario = kc.scenario(size)
    root = paths.state_root({"AI_HARNESS_HOME": str(tmp_path / "state")})
    paths.write_private(root / "knowledge" / "config.json", json.dumps(scenario.config))
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(scenario.corpus, ensure_ascii=False))
    result = ingest.update(root, build("file", {"input": path}), now="2026-11-27T00:00:00Z")
    traps = scenario.traps["rejected"]
    assert result["counts"]["rejected"] == sum(len(keys) for keys in traps.values())
    assert result["counts"]["indexed"] == size - result["counts"]["rejected"]
    with store.reader(root) as reader:
        assert sum(reader.status_counts().values()) == result["counts"]["indexed"]


def test_the_script_writes_the_files(tmp_path):
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "examples.synthetic.knowledge_corpus",
            str(tmp_path),
            "--size",
            "50",
        ],
        cwd=RUNTIME,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    written = json.loads(done.stdout)
    assert set(written) == {"config", "corpus", "tasks", "sources", "traps"}
    corpus = json.loads(Path(written["corpus"]).read_text())
    assert len(corpus["items"]) == 50
    assert records.violations("knowledge-record", corpus, "source_file") == []
    assert len(json.loads(Path(written["tasks"]).read_text())) == 30


# Paraphrase cases ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def paraphrased():
    return kc.paraphrase_scenario(50)


def searchable_terms(item):
    record = model.normalize(item, "2026-11-27T00:00:00Z")
    return set(terms.terms(store._searchable_text(record)))


def test_at_least_ten_paraphrase_cases_in_each_category(paraphrased):
    cases = paraphrased.paraphrase_cases
    counts = {name: sum(c["category"] == name for c in cases) for name in kc.PARAPHRASE_CATEGORIES}
    assert set(counts) == {"ja", "en", "cross"} and min(counts.values()) >= 10
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    assert {c["category"] for c in cases} == set(kc.PARAPHRASE_CATEGORIES)


@pytest.mark.parametrize("size", kc.SIZES)
def test_the_paraphrase_records_come_after_the_scenario_and_leave_it_unchanged(size):
    base, made = kc.scenario(size), kc.paraphrase_scenario(size)
    assert made.corpus["items"][:size] == base.corpus["items"]
    assert len(made.corpus["items"]) > size
    assert made.tasks == base.tasks and made.traps == base.traps
    assert {k: v for k, v in made.sources.items() if k in base.sources} == base.sources
    again = kc.paraphrase_scenario(size)
    assert records.canonical(again.corpus) == records.canonical(made.corpus)
    assert again.paraphrase_cases == made.paraphrase_cases
    assert base.paraphrase_cases == []


def test_each_case_names_relevant_advisory_records_of_its_topic(paraphrased):
    items = {
        model.record_id(i["origin"]["kind"], i["origin"]["key"]): i
        for i in paraphrased.corpus["items"]
    }
    for case in paraphrased.paraphrase_cases:
        assert case["relevant"], case["id"]
        for record_id in case["relevant"]:
            found = items[record_id]
            assert case["topic"] in found["facets"]["topic"], case["id"]
            assert found["enforcement"] != "mandatory" and found["status"] == "approved"
            assert found["scope"]["project_id"] in (None, case["project_id"])
        assert case["literal_query"] == kc.wording_of(case["topic"])[1]
        assert case["component_id"] != kc.OVERFLOW[1]


def test_the_language_of_each_category(paraphrased):
    def japanese(text):
        return any("぀" <= ch <= "鿿" for ch in text)

    for case in paraphrased.paraphrase_cases:
        records_ja = kc.wording_of(case["topic"])[0] == "ja"
        query_ja = japanese(case["query"])
        if case["category"] == "ja":
            assert records_ja and query_ja, case["id"]
        elif case["category"] == "en":
            assert not records_ja and not query_ja, case["id"]
        else:
            assert records_ja != query_ja, case["id"]


def test_a_paraphrase_shares_no_search_term_with_any_record(paraphrased):
    """Not with its wording, its relevant records, nor any other record: only another way of
    searching finds them, and nothing is found by chance."""
    every = [searchable_terms(i) for i in paraphrased.corpus["items"] if i.get("access")]
    for case in paraphrased.paraphrase_cases:
        words = set(terms.terms(case["query"]))
        assert words, case["id"]
        assert not words & set(terms.terms(case["literal_query"])), case["id"]
        assert not any(words & found for found in every), case["id"]


def test_the_limited_principal_is_never_given_a_third_project_record(paraphrased):
    gamma = {
        model.record_id(i["origin"]["kind"], i["origin"]["key"])
        for i in paraphrased.corpus["items"]
        if i["scope"]["project_id"] == kc.THIRD_PROJECT
    }
    limited = [c for c in paraphrased.paraphrase_cases if c["principal"] == kc.PRINCIPAL_LIMITED]
    assert limited
    assert all(not set(c["relevant"]) & gamma for c in limited)
