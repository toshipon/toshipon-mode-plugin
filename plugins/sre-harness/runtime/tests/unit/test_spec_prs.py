"""The PR list, the bare repository's git commands, the diff cache and the diff reader."""

import json
from types import SimpleNamespace

import pytest

from examples.synthetic.spec_scenarios import _http
from sre_harness import guard, paths
from sre_harness.errors import BoundaryError, InputError, QualityGateError
from sre_harness.github import fixture as transport
from sre_harness.github.rest import Limiter
from sre_harness.spec import prs

SHA, OTHER = "a" * 40, "b" * 40

DIFF = """\
diff --git a/docs/a.md b/docs/a.md
index 1111111..2222222 100644
--- a/docs/a.md
+++ b/docs/a.md
@@ -3 +3 @@ heading
-- days: 1
+- days: 2
@@ -10,0 +11,2 @@
+x
+y
diff --git a/new file.md b/new file.md
new file mode 100644
--- /dev/null
+++ b/new file.md
@@ -0,0 +1,3 @@
+a
+b
+c
diff --git "a/docs/\\346\\227\\245.md" "b/docs/\\346\\227\\245.md"
deleted file mode 100644
--- "a/docs/\\346\\227\\245.md"
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


def test_the_diff_reader_keeps_paths_status_and_ranges():
    files = prs.parse_diff(DIFF)
    assert [(f["path"], f["status"]) for f in files] == [
        ("docs/a.md", "M"),
        ("new file.md", "A"),
        ("docs/日.md", "D"),
        ("img.png", "M"),
    ]
    assert files[0]["base"] == [(3, 1), (10, 0)] and files[0]["head"] == [(3, 3), (11, 12)]
    assert files[1]["head"] == [(1, 3)] and files[2]["head"] == []
    assert prs.removed_ranges(files[0]["base"]) == [(3, 3)]
    assert prs.unquote("plain") == "plain"


def test_overlap_is_what_a_three_way_merge_would_conflict_on():
    assert prs.overlaps([(3, 1)], [(3, 1)])
    assert prs.overlaps([(3, 1)], [(4, 1)])  # adjacent lines
    assert not prs.overlaps([(3, 1)], [(5, 1)])
    assert prs.overlaps([(3, 0)], [(3, 1)])  # an insertion right after a changed line
    assert not prs.overlaps([(3, 0)], [(10, 2)])


def _fixture(directory, pages):
    directory.mkdir()
    index = {}
    for number, (items, headers) in enumerate(pages, start=1):
        key = transport.rest_key(
            "pulls",
            {"owner": "example-org", "repo": "demo"},
            {"state": "open", "per_page": 100, "page": number},
        )
        name = f"s{number:04d}.http"
        (directory / name).write_text(_http(200, items, headers))
        index[key] = [name]
    (directory / transport.INDEX).write_text(json.dumps({"responses": index}))
    return transport.FixtureTransport(directory)


def _item(number, sha=SHA, **extra):
    return {
        "number": number,
        "title": f"PR {number}",
        "head": {"sha": sha, "repo": {"full_name": "example-org/demo"}},
        "base": {"ref": "main", "sha": OTHER, "repo": {"default_branch": "main"}},
        **extra,
    }


def test_the_open_list_follows_pages_and_keeps_gaps(tmp_path):
    link = (
        '<https://api.github.com/repositories/1/pulls?state=open&per_page=100&page=2>; rel="next"'
    )
    served = _fixture(
        tmp_path / "fx",
        [
            ([_item(1), _item(2, sha="short"), "junk"], {"Link": link}),
            ([_item(3, head={"sha": OTHER, "repo": None})], {}),
        ],
    )
    pulls, gaps = prs.list_open(
        served, Limiter(lambda: 0, lambda s: None, 0), "example-org", "demo"
    )
    assert [p["number"] for p in pulls] == [1, 3]
    assert pulls[0]["default_branch"] == "main" and pulls[0]["base_sha"] == OTHER
    assert pulls[1]["head_repo_missing"] is True
    assert gaps == [{"number": 2, "reason": "no_head_sha"}, {"reason": "not_an_object"}]


def test_an_unreadable_list_stops_the_stage(tmp_path):
    directory = tmp_path / "fx"
    directory.mkdir()
    key = transport.rest_key(
        "pulls",
        {"owner": "example-org", "repo": "demo"},
        {"state": "open", "per_page": 100, "page": 1},
    )
    (directory / "s0001.http").write_text(_http(404, {"message": "Not Found"}))
    (directory / transport.INDEX).write_text(json.dumps({"responses": {key: ["s0001.http"]}}))
    with pytest.raises(QualityGateError):
        prs.list_open(
            transport.FixtureTransport(directory),
            Limiter(lambda: 0, lambda s: None, 0),
            "example-org",
            "demo",
        )


@pytest.mark.parametrize("name", ["main", "release/1.2", "feature_x"])
def test_branch_names_that_are_read(name):
    assert prs.branch_name(name) == name


@pytest.mark.parametrize("name", ["", "-x", "a..b", "/a", "a/", "a b", None, "x.lock."])
def test_branch_names_that_are_refused(name):
    with pytest.raises(InputError):
        prs.branch_name(name)


@pytest.fixture
def git_calls(monkeypatch):
    """Record git's argv instead of starting it; answer from a queue."""
    seen, answers = [], []

    def run(argv, **options):
        seen.append(SimpleNamespace(argv=list(argv), env=options["env"]))
        code, out = answers.pop(0) if answers else (0, b"")
        return SimpleNamespace(returncode=code, stdout=out, stderr=b"")

    monkeypatch.setattr(guard, "_run", run)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    return seen, answers


def test_every_git_command_the_stage_uses_passes_the_guard(tmp_path, git_calls):
    seen, answers = git_calls
    root = tmp_path / "state"
    source = tmp_path / "src"
    source.mkdir()
    repository = prs.Repository.open(root, "example-org", "demo", str(source))
    assert repository.git_dir.parent == root / "cache" / "git"
    answers += [(0, b""), (0, SHA.encode() + b"\n"), (0, OTHER.encode() + b"\n")]
    answers += [(0, b"A\tdocs/a.md\n"), (0, b"text"), (0, b"")]
    assert repository.fetch_open("main", [7, 8]) == {7, 8}
    assert repository.rev_parse(prs.pull_ref(7)) == SHA
    assert repository.merge_base(SHA, OTHER) == OTHER
    assert repository.files(SHA) == ["docs/a.md"]
    assert repository.blob(SHA, "docs/a.md") == "text"
    assert repository.blob(SHA, "docs/a.md") == "text"  # read once
    prs.DiffCache(repository, root / "run").get(OTHER, SHA)
    subcommands = [call.argv[1] for call in seen]
    assert subcommands == ["init", "fetch", "rev-parse", "merge-base", "diff", "cat-file", "diff"]
    assert seen[1].argv[-4:] == [
        str(source),
        "+refs/heads/main:refs/sre-harness/heads/main",
        "+refs/pull/7/head:refs/sre-harness/pull/7",
        "+refs/pull/8/head:refs/sre-harness/pull/8",
    ]
    for call in seen:
        assert guard.check_git(call.argv[1:]) == call.argv[1:]
        assert call.env["GIT_CONFIG_GLOBAL"] == guard.os.devnull
    assert repository.calls == len(seen)


def test_a_failed_fetch_falls_back_to_one_fetch_per_pr(tmp_path, git_calls):
    seen, answers = git_calls
    source = tmp_path / "src"
    source.mkdir()
    repository = prs.Repository.open(tmp_path / "state", "example-org", "demo", str(source))
    answers += [(128, b""), (0, b""), (0, b""), (128, b"")]
    assert repository.fetch_open("main", [7, 8]) == {7}
    assert [call.argv[-1] for call in seen[2:]] == [
        "+refs/heads/main:refs/sre-harness/heads/main",
        "+refs/pull/7/head:refs/sre-harness/pull/7",
        "+refs/pull/8/head:refs/sre-harness/pull/8",
    ]
    answers += [(128, b""), (128, b"")]
    with pytest.raises(QualityGateError):
        repository.fetch_open("main", [9])


def test_the_guard_still_refuses_what_the_stage_never_sends(tmp_path, git_calls):
    seen, _ = git_calls
    for arguments in (
        ["fetch", str(tmp_path), "+refs/pull/7/head:refs/heads/main"],
        ["fetch", "relative/path", "+refs/heads/main:refs/sre-harness/heads/main"],
        ["cat-file", "blob", f"{SHA}:../etc"],
        ["diff", "--unified=0", "HEAD", SHA],
        ["worktree", "add", str(tmp_path), SHA],
        ["checkout", SHA],
    ):
        with pytest.raises(BoundaryError):
            guard.git(tmp_path, arguments)
    assert seen == []


class _Repo:
    def __init__(self, data=b"diff --git a/x b/x\n"):
        self.data, self.calls = data, []

    def run(self, arguments):
        self.calls.append(arguments)
        return 0, self.data


def test_the_diff_cache_computes_each_pair_once(tmp_path):
    repository = _Repo()
    cache = prs.DiffCache(repository, tmp_path / "run")
    first = cache.get(SHA, OTHER)
    assert cache.get(SHA, OTHER) == first
    cache.get(OTHER, SHA)
    assert (cache.computed, cache.reused) == (2, 1)
    assert len(repository.calls) == 2
    stored = sorted(p.name for p in (tmp_path / "run" / "diff-cache").iterdir())
    assert len(stored) == 2 and all(len(name) == 64 + 5 for name in stored)
    assert oct((tmp_path / "run" / "diff-cache" / stored[0]).stat().st_mode & 0o777) == "0o600"

    again = prs.DiffCache(repository, tmp_path / "run")
    again.get(SHA, OTHER)
    assert (again.computed, again.reused) == (0, 1) and len(repository.calls) == 2


def test_a_diff_over_the_cap_stops_instead_of_being_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(prs, "MAX_DIFF_BYTES", 10)
    cache = prs.DiffCache(_Repo(b"x" * 11), tmp_path / "run")
    with pytest.raises(QualityGateError):
        cache.get(SHA, OTHER)
    assert not (tmp_path / "run" / "diff-cache").exists()
    monkeypatch.setattr(prs, "MAX_DIFF_BYTES", 100)
    monkeypatch.setattr(prs, "MAX_RUN_DIFF_BYTES", 15)
    cache = prs.DiffCache(_Repo(b"x" * 10), tmp_path / "run2")
    cache.get(SHA, OTHER)
    with pytest.raises(QualityGateError):
        cache.get(OTHER, SHA)


def test_a_blob_over_the_cap_stops(tmp_path, git_calls, monkeypatch):
    _, answers = git_calls
    monkeypatch.setattr(prs, "MAX_BLOB_BYTES", 3)
    repository = prs.Repository(tmp_path / "r.git", str(tmp_path))
    answers += [(0, b"abcd")]
    with pytest.raises(QualityGateError):
        repository.blob(SHA, "docs/a.md")
    answers += [(0, b"\xff\xfe"), (128, b"")]
    assert repository.blob(SHA, "docs/b.md") is None  # not UTF-8
    assert repository.blob(SHA, "docs/c.md") is None  # absent
    assert "token" not in repr(repository).lower()
    assert paths.RUN_ID  # paths stays importable next to prs
