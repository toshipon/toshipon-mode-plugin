"""The patch gate without git: the path allowlist, the glob, and the diff header parser.

Every refused case is a way a Unified Diff could make git touch something other than the
document the harness meant: the parser and the allowlist must refuse it before git runs.
"""

import pytest

from sre_harness.spec import patch

ALLOW = patch.Allow(documents=frozenset({"docs/requirements.md"}), globs=("docs/**/*.md",))


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../x.md", "dotdot"),
        ("a/../../x.md", "dotdot"),
        ("docs/./x.md", "dotdot"),
        ("/etc/x.md", "not_a_plain_path"),
        ("~/x.md", "not_a_plain_path"),
        ("docs//x.md", "not_a_plain_path"),
        ('"docs/x.md"', "not_a_plain_path"),
        ("docs\\x.md", "not_a_plain_path"),
        ("docs/x y.md", "not_a_plain_path"),
        ("docs/x\t.md", "not_a_plain_path"),
        ("", "not_a_plain_path"),
        (".git/config", "git_dir"),
        ("sub/.git/hooks/x", "git_dir"),
        (".GIT/HEAD", "git_dir"),
        (".gitmodules", "git_dir"),
        (".github/workflows/ci.yml", "github_dir"),
        (".GitHub/workflows/notes.md", "github_dir"),
        ("docs/.github/x.md", "github_dir"),
        ("infra/logs.tf", "iac_or_code"),
        ("src/app/handler.py", "iac_or_code"),
        ("tests/test_retention.py", "iac_or_code"),
        ("docs/notes.txt", "iac_or_code"),
        ("README.md", "not_allowed"),
        ("notes/x.md", "not_allowed"),
    ],
)
def test_paths_outside_the_allowlist_are_refused(path, code):
    refusal = patch.path_refusal(path, ALLOW)
    assert refusal is not None and refusal[0] == code, refusal


@pytest.mark.parametrize(
    "path", ["docs/requirements.md", "docs/design/retention-note.md", "docs/a/b/c.md"]
)
def test_manifest_documents_and_glob_matches_are_allowed(path):
    assert patch.path_refusal(path, ALLOW) is None


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("docs/**/*.md", "docs/x.md", True),
        ("docs/**/*.md", "docs/a/b/x.md", True),
        ("docs/*.md", "docs/a/x.md", False),
        ("docs/*.md", "docs/x.md", True),
        ("**/*.md", "x.md", True),
        ("docs/**/*.md", "other/x.md", False),
        ("docs/design/*.md", "docs/design/x.markdown", False),
    ],
)
def test_globs_match_by_path_segment(pattern, path, expected):
    assert patch.glob_match(pattern, path) is expected


def _diff(path="docs/requirements.md", head=None, old="ADR-0002", new="ADR-0005", extra=""):
    head = head or f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    return (head + extra + f"@@ -1,3 +1,3 @@\n a\n-{old} x\n+{new} x\n c\n").encode()


def test_a_plain_diff_passes_the_screen():
    assert patch.screen(_diff(), ALLOW) == ["docs/requirements.md"]
    indexed = _diff(extra="")
    header = "diff --git a/docs/requirements.md b/docs/requirements.md\n"
    with_index = indexed.replace(
        header.encode(), (header + "index 0123456..89abcde 100644\n").encode()
    )
    assert patch.screen(with_index, ALLOW) == ["docs/requirements.md"]


def _replace_header(text):
    return _diff(head=text)


P = "docs/requirements.md"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        # Paths a reader and git could read differently, or that leave the allowlist.
        (_diff("../x.md"), "dotdot"),
        (_diff("a/../../x.md"), "dotdot"),
        (
            _replace_header(
                "diff --git a//etc/x.md b//etc/x.md\n--- a//etc/x.md\n+++ b//etc/x.md\n"
            ),
            "not_a_plain_path",
        ),
        (_diff(".git/config"), "git_dir"),
        (_diff("sub/.git/hooks/x"), "git_dir"),
        (_diff(".github/workflows/ci.yml"), "github_dir"),
        (_diff(".GitHub/workflows/notes.md"), "github_dir"),
        (_diff("infra/logs.tf"), "iac_or_code"),
        (_diff("src/app/handler.py"), "iac_or_code"),
        (_diff("README.md"), "not_allowed"),
        (_replace_header(f'diff --git "a/{P}" "b/{P}"\n--- "a/{P}"\n+++ "b/{P}"\n'), "quoted_path"),
        (
            _replace_header(
                "diff --git a/docs/a\\b.md b/docs/a\\b.md\n--- a/docs/a\\b.md\n+++ b/docs/a\\b.md\n"
            ),
            "not_a_plain_path",
        ),
        (
            _replace_header(f"diff --git a/{P} b/{P}\n--- a/{P}\t2026-01-01 00:00:00\n+++ b/{P}\n"),
            "header_mismatch",
        ),
        (_replace_header(f"diff --git {P} {P}\n--- {P}\n+++ {P}\n"), "no_prefix"),
        (
            _replace_header(f"diff --git a/{P} b/docs/other.md\n--- a/{P}\n+++ b/docs/other.md\n"),
            "header_mismatch",
        ),
        (
            _replace_header(f"diff --git a/{P} b/{P}\n--- a/{P}\n+++ b/.github/workflows/ci.yml\n"),
            "header_mismatch",
        ),
        # Extended headers: rename, copy, new and deleted files, modes, symlinks, binary.
        (
            _replace_header(
                f"diff --git a/{P} b/{P}\nsimilarity index 90%\nrename from {P}\nrename to .github/workflows/ci.yml\n--- a/{P}\n+++ b/{P}\n"
            ),
            "extended_header",
        ),
        (
            _replace_header(
                f"diff --git a/{P} b/{P}\ncopy from {P}\ncopy to docs/x.md\n--- a/{P}\n+++ b/{P}\n"
            ),
            "extended_header",
        ),
        (
            _replace_header(
                "diff --git a/docs/new.md b/docs/new.md\nnew file mode 100644\n--- /dev/null\n+++ b/docs/new.md\n"
            ),
            "extended_header",
        ),
        (
            _replace_header(f"diff --git a/{P} b/{P}\n--- /dev/null\n+++ b/{P}\n"),
            "new_or_deleted_file",
        ),
        (
            _replace_header(
                f"diff --git a/{P} b/{P}\ndeleted file mode 100644\n--- a/{P}\n+++ /dev/null\n"
            ),
            "extended_header",
        ),
        (
            _replace_header(
                f"diff --git a/{P} b/{P}\nold mode 100644\nnew mode 100755\n--- a/{P}\n+++ b/{P}\n"
            ),
            "extended_header",
        ),
        (
            _replace_header(
                f"diff --git a/{P} b/{P}\nindex 0123456..89abcde 120000\n--- a/{P}\n+++ b/{P}\n"
            ),
            "mode",
        ),
        (
            _replace_header(
                f"diff --git a/{P} b/{P}\nnew file mode 120000\n--- /dev/null\n+++ b/{P}\n"
            ),
            "extended_header",
        ),
        (
            f"diff --git a/{P} b/{P}\nindex 0123456..89abcde\nGIT binary patch\nliteral 3\nKcmZ>\n\n".encode(),
            "mode",
        ),
        (f"diff --git a/{P} b/{P}\nGIT binary patch\nliteral 3\nKcmZ>\n\n".encode(), "binary"),
        (f"diff --git a/{P} b/{P}\nBinary files a/{P} and b/{P} differ\n".encode(), "binary"),
        # Text git would read as another patch, and broken hunks.
        (
            b"--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n",
            "unexpected_line",
        ),
        (b"note\n" + _diff(), "unexpected_line"),
        (
            _diff() + b"--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n",
            "unexpected_line",
        ),
        (_diff()[:-3], "truncated_hunk"),
        (f"diff --git a/{P} b/{P}\n--- a/{P}\n+++ b/{P}\n".encode(), "no_hunk"),
        (_diff() + _diff(), "path_twice"),
        (_diff().replace(b"\n c\n", b"\r\n c\n"), "not_a_plain_text"),
        (_diff()[:-1], "not_a_plain_text"),
        (b"\xff\xfe" + _diff(), "not_a_plain_text"),
        (b"", "no_hunk"),
    ],
)
def test_the_screen_refuses_headers_git_could_read_another_way(data, code):
    with pytest.raises(patch.Refused) as raised:
        patch.screen(data, ALLOW)
    assert raised.value.code == code, raised.value


def test_a_patch_over_the_cap_stops_instead_of_being_cut():
    from sre_harness.errors import QualityGateError

    with pytest.raises(QualityGateError):
        patch.screen(b"x" * (patch.MAX_PATCH_BYTES + 1), ALLOW)


@pytest.mark.parametrize("value", ["PT-0001", "PT-12345"])
def test_patch_ids_are_the_only_names_accepted(value):
    assert patch.patch_id(value) == value


@pytest.mark.parametrize("value", ["../x", "PT-1", "PT-0001.diff", "pt-0001", "PT-0001/..", ""])
def test_anything_else_is_refused_as_a_patch_name(value):
    from sre_harness.errors import BoundaryError

    with pytest.raises(BoundaryError):
        patch.patch_id(value)


def test_blob_sha_is_the_git_object_id():
    # `printf 'hello\n' | git hash-object --stdin`
    assert patch.blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def _registry():
    from sre_harness.spec import trace
    from tests.spec_helpers import decision

    decisions = [
        decision(2, status="superseded", superseded_by=["DEC-0005"]),
        decision(5, supersedes=["DEC-0002"]),
        decision(3, status="superseded", superseded_by=["DEC-0004"]),
        decision(4, status="superseded", superseded_by=["DEC-0006"]),
        decision(6),
        decision(7, status="superseded", superseded_by=["DEC-0008", "DEC-0009"]),
        decision(8),
        decision(9),
        decision(10, status="superseded", superseded_by=["DEC-0011"]),
        decision(11, status="rejected"),
    ]
    return trace.Registry(decisions, [])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("DEC-0002", "DEC-0005"),
        ("DEC-0003", "DEC-0006"),  # the end of the chain, not the next step
        ("DEC-0007", None),  # two successors: people decide
        ("DEC-0010", None),  # a rejected decision is no successor
        ("DEC-0005", None),  # not superseded
    ],
)
def test_a_reference_is_moved_only_to_the_single_active_successor(name, expected):
    assert patch.successor(_registry(), name) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("- retention_days: 365", "- retention_days: 400"),
        ("  * retention_days:   365  ", "  * retention_days:   400  "),
        ('- retention_days: "365"', '- retention_days: "400"'),
        ("retention_days: 365", "retention_days: 400"),
        ("- archive_retention_days: 365", None),
        ("retention days are 365", None),
    ],
)
def test_only_a_line_that_states_the_key_gets_the_registry_value(line, expected):
    assert patch._apply_rule(line, ("value", "retention_days", "400")) == expected


def test_a_reference_rule_replaces_whole_ids_only():
    rule = ("reference", {"ADR-0002": "ADR-0005", "DEC-0002": "DEC-0005"})
    assert patch._apply_rule("Decisions: ADR-0002, DEC-0002", rule) == (
        "Decisions: ADR-0005, DEC-0005"
    )
    assert patch._apply_rule("Decisions: ADR-00021", rule) is None
    assert patch._apply_rule("XADR-0002", rule) is None
