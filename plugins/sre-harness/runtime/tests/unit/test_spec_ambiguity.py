"""Rule-based ambiguity checks. The vocabulary is a table; every row is tested in context."""

import pytest

from sre_harness.spec import ambiguity

CRITERION = {"id": "AC1", "given": "", "when": "", "then": "it works", "measurable": False}


def requirement(statement, criteria=(CRITERION,), category=None):
    return {"statement": statement, "acceptance_criteria": list(criteria), "nfr_category": category}


def kinds(found):
    return sorted({entry["kind"] for entry in found})


@pytest.mark.parametrize("term,language", ambiguity.VAGUE_TERMS)
def test_every_vague_term_in_the_table_is_found(term, language):
    statement = (
        f"The service responds {term} here." if language == "en" else f"API は{term}応答する。"
    )
    found = ambiguity.detect(requirement(statement))
    assert any(e["kind"] == "vague_term" and e["span"].lower() == term.lower() for e in found)
    assert all(entry["detector"] == "rule" for entry in found)


@pytest.mark.parametrize("term,language", ambiguity.UNBOUNDED_TERMS)
def test_every_unbounded_term_in_the_table_is_found(term, language):
    statement = (
        f"Logs are kept {term} for audit." if language == "en" else f"ログは{term}保持する。"
    )
    found = ambiguity.detect(requirement(statement))
    assert any(e["kind"] == "unbounded" and e["span"].lower() == term.lower() for e in found)


def test_the_table_covers_japanese_and_english_only():
    languages = {language for _, language in ambiguity.VAGUE_TERMS + ambiguity.UNBOUNDED_TERMS}
    assert languages == {"ja", "en"}
    for term in ("適切に", "できるだけ", "高速", "as appropriate", "fast"):
        assert term in {entry for entry, _ in ambiguity.VAGUE_TERMS}


@pytest.mark.parametrize(
    "statement",
    [
        "Breakfast is served at the office.",  # "fast" inside a word
        "The steadfast check stays.",
        "Logs are kept for 400 days.",
    ],
)
def test_english_terms_match_whole_words_only(statement):
    assert "vague_term" not in kinds(ambiguity.detect(requirement(statement)))


@pytest.mark.parametrize(
    "statement",
    [
        "アプリケーションログを 400 日保持しなければならない。",
        "A restore drill must finish within 4 hours.",
        "p99 latency stays under 300 ms.",
        "Error rate stays below 0.1%.",
        "Each archive is at most 2 GiB.",
    ],
)
def test_a_number_with_a_unit_is_not_no_metric(statement):
    found = ambiguity.detect(requirement(statement, category="performance"))
    assert "no_metric" not in kinds(found)


def test_no_metric_for_a_quality_requirement_without_number_and_unit():
    found = ambiguity.detect(requirement("API は高速に応答すること。", category="performance"))
    assert {"vague_term", "no_metric"} <= set(kinds(found))


def test_no_metric_is_not_raised_for_a_requirement_without_a_quality_category():
    found = ambiguity.detect(requirement("The export includes the request id."))
    assert "no_metric" not in kinds(found)


def test_a_number_in_a_criterion_counts_as_a_metric():
    measured = {**CRITERION, "then": "the call fails within 10 s", "measurable": True}
    found = ambiguity.detect(requirement("Calls time out.", [measured], category="availability"))
    assert "no_metric" not in kinds(found)


def test_no_criteria_when_acceptance_criteria_are_absent():
    found = ambiguity.detect(requirement("The export includes the request id.", criteria=()))
    assert kinds(found) == ["no_criteria"]


@pytest.mark.parametrize(
    "statement",
    [
        "The export must include the request id and must be signed.",
        "ログを保存しなければならず、30 日後に削除しなければならない。",
    ],
)
def test_two_requirements_in_one_sentence_are_compound(statement):
    assert "compound" in kinds(ambiguity.detect(requirement(statement)))


def test_two_sentences_are_not_compound():
    statement = "The export must include the request id. The export must be signed."
    assert "compound" not in kinds(ambiguity.detect(requirement(statement)))


def test_spans_are_substrings_of_the_statement():
    statement = "Logs are always kept as appropriate."
    for entry in ambiguity.detect(requirement(statement)):
        if entry.get("span"):
            assert entry["span"] in statement
            assert entry["field"] == "statement"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("400 日より古いログだけが削除される", (400.0, "日")),
        ("within 4 hours", (4.0, "hours")),
        ("under 300 ms", (300.0, "ms")),
        ("below 0.1%", (0.1, "%")),
        ("15 分以内に検知", (15.0, "分")),
        ("version 2 of the API", None),
        ("no number", None),
    ],
)
def test_measure_reads_a_target_and_a_unit(text, expected):
    assert ambiguity.measure(text) == expected
