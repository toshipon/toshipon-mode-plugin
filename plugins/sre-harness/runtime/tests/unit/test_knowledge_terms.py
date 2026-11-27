import pytest

from sre_harness.knowledge import terms


def test_words_are_lowercased_and_japanese_is_cut_into_bigrams():
    assert terms.terms("Queue の保持期間 retention_days=14") == sorted(
        {"queue", "retention_days", "14", "の保", "保持", "持期", "期間"}
    )


def test_single_characters_are_not_terms():
    assert terms.terms("a の x") == []


def test_terms_are_sorted_and_unique():
    assert terms.terms("保持 保持 b2 a1 B2") == ["a1", "b2", "保持"]


def test_katakana_and_kanji_runs_are_cut_alike():
    assert terms.terms("タイムアウト") == ["アウ", "イム", "ウト", "タイ", "ムア"]


def test_query_syntax_survives_only_as_words():
    assert terms.terms('"保持" NEAR(a b) OR col:x*') == sorted({"保持", "near", "or", "col"})


@pytest.mark.parametrize(
    ("text", "tokens"),
    [("", 0), ("a", 1), ("abc", 1), ("abcd", 2), ("保", 1), ("保持", 2), (b"abcdef", 2)],
)
def test_tokens_are_three_utf8_bytes_each_rounded_up(text, tokens):
    assert terms.estimate_tokens(text) == tokens


def test_the_estimator_has_a_name_for_the_output():
    assert terms.ESTIMATOR == "utf8_bytes_div_3"
