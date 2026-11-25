import pytest

from sre_harness import privacy
from sre_harness.errors import BoundaryError, InputError


def test_deny_terms_use_the_improvement_harness_format(tmp_path):
    path = tmp_path / "deny-terms.txt"
    path.write_text("# customers\nExampleCorp\n\n  internal.example  \n")
    terms = privacy.load_deny_terms(path)
    assert terms == ["examplecorp", "internal.example"]
    assert privacy.find_deny_terms("ask EXAMPLECORP about it", terms) == ["examplecorp"]
    assert privacy.load_deny_terms(None) == []


def test_a_missing_deny_terms_file_is_an_input_error(tmp_path):
    with pytest.raises(InputError):
        privacy.load_deny_terms(tmp_path / "missing.txt")


def test_text_with_a_denied_term_is_not_sent_and_the_term_is_not_echoed():
    with pytest.raises(BoundaryError) as refused:
        privacy.assert_sendable("a note about ExampleCorp", ["examplecorp"], "the review threads")
    assert refused.value.exit_code == 3
    assert "examplecorp" not in str(refused.value).lower()
    privacy.assert_sendable("nothing to see", ["examplecorp"], "the review threads")


def test_secret_shapes_are_named():
    fake = "ghp_" + "A" * 36
    assert privacy.find_secrets(f"token {fake}") == ["github_token"]
    assert privacy.find_secrets("https://user:hunter2@example.com/x") == ["url_credentials"]
    assert privacy.find_secrets("max_tokens = 4000") == []
