"""Build a synthetic git repository with open PRs, and the GitHub PR list that describes it.

Everything is made up (the `log-pipeline` system, the files, the values); hosts are reserved
(`example.com`, `*.invalid`). The repository is written to a directory the caller gives
(a temporary one in tests). git runs with `GIT_CONFIG_GLOBAL=/dev/null`, no system config and
fixed author, committer and dates, so the commits and their SHAs are the same on every machine.
This module may start git itself: it is example code, not part of `sre_harness`.

    scenario = build_parallel(tmp_path / "p")        # the 25 open PRs below
    scenario.source   # repository to pass as --git-source (refs/pull/<n>/head exist)
    scenario.fixture  # directory to pass as --fixture (pulls?state=open)
    scenario.manifest # manifest for `spec ingest` (the same files as the default branch)
    advance_pull(scenario.source, 101)  # move a PR head after the list was written

The 25 open PRs (numbers 101-125), all against `main`:

- 101/102, 103/104, 105/106: three pairs. Each pair sets one retention key of REQ-0001 to two
  different values in two different files (a document and `infra/logs.tf`), so git merges the
  pair without a conflict and the values disagree: three `semantic_conflict` findings.
  Every PR of a pair also adds a test named after REQ-0001.
- 107 adds a design document that follows ADR-0002, which ADR-0005 superseded:
  `superseded_dependency`.
- 108 changes REQ-0004's `timeout_seconds` in docs/requirements.md and 109 its `max_retries` in
  `infra/http.tf`; neither changes a test: two `missing_test` findings.
- 110-125 touch files that no decision, requirement or test traces to; 124 and 125 share one
  head commit (one diff for both). 110's title carries Markdown and HTML to check the report's
  escaping. None of these 16 may get a finding.

Run as a script to write it: `python -m examples.synthetic.spec_repo OUT_DIR` from `runtime/`.
"""

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sre_harness.github import fixture as transport

from .spec_scenarios import REPO, RETENTION_DAYS, _http, write_workspace

DEFAULT_BRANCH = "main"
PAIRS = ((101, 102), (103, 104), (105, 106))
SUPERSEDED_PR = 107
MISSING_TEST_PRS = (108, 109)
CLEAN_PRS = tuple(range(110, 126))
SAME_HEAD = (124, 125)
ALL_PRS = tuple(range(101, 126))
MARKDOWN_TITLE = (
    "ハンドラを整理する [x](http://example.invalid/a) ![](http://example.invalid/i.png) "
    "| `code` <b>bold</b>"
)
_DATE = "2026-01-20T00:00:00Z"

LOGS_TF = f"""\
# Synthetic IaC text for the log pipeline. It is never applied.

# Implements REQ-0001 (hot storage).
resource "aws_s3_bucket_lifecycle_configuration" "app_logs" {{
  bucket         = "logs-app"
  retention_days = {RETENTION_DAYS} # REQ-0001
}}

resource "aws_s3_bucket" "app_logs" {{
  bucket = "logs-app"
}}

# Implements REQ-0001 (archive).
resource "aws_s3_bucket_lifecycle_configuration" "archive" {{
  bucket                 = "logs-archive"
  archive_retention_days = 1825 # REQ-0001
}}

resource "aws_s3_bucket" "archive" {{
  bucket = "logs-archive"
}}

# Implements REQ-0001 (audit).
resource "aws_s3_bucket_lifecycle_configuration" "audit" {{
  bucket               = "logs-audit"
  audit_retention_days = 400 # REQ-0001
}}
"""

HTTP_TF = """\
# Implements REQ-0004.
resource "synthetic_http_client" "outbound" {
  timeout_seconds = 10 # REQ-0004
  max_retries     = 0  # REQ-0004
}
"""

LOG_STORAGE = """\
# ログ保存の設計

## アーカイブの保持（REQ-0001）

- archive_retention_days: 1825

実装は `infra/logs.tf` の `aws_s3_bucket_lifecycle_configuration.archive` です。
"""

AUDIT_LOG = """\
# 監査ログの設計

## 監査ログの保持（REQ-0001）

- audit_retention_days: 400

実装は `aws_s3_bucket_lifecycle_configuration.audit` です。
"""

DASHBOARD = """\
# ダッシュボードの設計

ログの保持期間を一覧で見られるようにする。
"""

RETENTION_TESTS = """\
def test_req_0001_deletes_logs_older_than_retention():
    assert True


def test_req_0001_archive_keeps_five_years():
    assert True
"""

HTTP_TESTS = """\
def test_req_0004_times_out():
    assert True
"""

RUNBOOK = """\
# 当番の手順

1. ページを受けたら 5 分以内に応答する。
2. 手順書のリンクを開く。
"""

UTILS_TESTS = """\
def test_parse_line():
    assert True
"""

BASE_FILES = {
    "README.md": "# demo\n\n合成のリポジトリです。\n",
    "infra/logs.tf": LOGS_TF,
    "infra/http.tf": HTTP_TF,
    "docs/design/log-storage.md": LOG_STORAGE,
    "docs/design/audit-log.md": AUDIT_LOG,
    "docs/design/log-dashboard.md": DASHBOARD,
    "docs/runbook/oncall.md": RUNBOOK,
    "tests/test_retention.py": RETENTION_TESTS,
    "tests/test_http.py": HTTP_TESTS,
    "tests/test_utils.py": UTILS_TESTS,
    "src/app/handler.py": "def handle(event):\n    return event\n",
    "src/app/shared.py": "VALUE = 1\n",
}


def _replace(path, old, new):
    text = BASE_FILES[path]
    assert old in text, (path, old)
    return text.replace(old, new, 1)


def _test(number, value):
    return {
        f"tests/test_retention_pr{number}.py": (
            f"def test_req_0001_keeps_{value}_days():\n    assert True\n"
        )
    }


def _retention(path, key, old, new):
    return {path: _replace(path, f"- {key}: {old}", f"- {key}: {new}")}


def _tf(key, old, new):
    text = BASE_FILES["infra/logs.tf"]
    marker = f"{key} = {old} # REQ-0001"
    assert marker in text, key
    return {"infra/logs.tf": text.replace(marker, f"{key} = {new} # REQ-0001", 1)}


def parallel_changes(requirements):
    """{number: {"title": ..., "files": {path: text or None}}} for the 25 open PRs."""
    pulls = {
        101: {
            "title": "保持期間を 365 日にする",
            "files": {
                "docs/requirements.md": requirements.replace(
                    f"- retention_days: {RETENTION_DAYS}", "- retention_days: 365", 1
                ),
                **_test(101, 365),
            },
        },
        102: {
            "title": "保持期間を 730 日にする",
            "files": {**_tf("retention_days", RETENTION_DAYS, 730), **_test(102, 730)},
        },
        103: {
            "title": "アーカイブを 3 年にする",
            "files": {
                **_retention("docs/design/log-storage.md", "archive_retention_days", 1825, 1095),
                **_test(103, 1095),
            },
        },
        104: {
            "title": "アーカイブを 10 年にする",
            "files": {**_tf("archive_retention_days", 1825, 3650), **_test(104, 3650)},
        },
        105: {
            "title": "監査ログを 180 日にする",
            "files": {
                **_retention("docs/design/audit-log.md", "audit_retention_days", 400, 180),
                **_test(105, 180),
            },
        },
        106: {
            "title": "監査ログを 7 年にする",
            "files": {**_tf("audit_retention_days", 400, 2555), **_test(106, 2555)},
        },
        SUPERSEDED_PR: {
            "title": "再試行の設計を足す",
            "files": {
                "docs/design/http-retry.md": (
                    "# 再試行の設計\n\n外部呼び出しの再試行は ADR-0002 の方針に合わせる。\n"
                )
            },
        },
        108: {
            "title": "タイムアウトを短くする",
            "files": {
                "docs/requirements.md": requirements.replace(
                    "- timeout_seconds: 10", "- timeout_seconds: 5", 1
                )
            },
        },
        109: {
            "title": "再試行を 2 回にする",
            "files": {
                "infra/http.tf": BASE_FILES["infra/http.tf"].replace(
                    "max_retries     = 0", "max_retries     = 2", 1
                )
            },
        },
        110: {
            "title": MARKDOWN_TITLE,
            "files": {"src/app/handler.py": "def handle(event):\n    return dict(event)\n"},
        },
        111: {
            "title": "当番の手順を足す",
            "files": {"docs/runbook/oncall.md": RUNBOOK + "3. 引き継ぎを書く。\n"},
        },
        112: {
            "title": "README を直す",
            "files": {"README.md": "# demo\n\n合成のリポジトリです。手順は docs にあります。\n"},
        },
        113: {
            "title": "テストを足す",
            "files": {
                "tests/test_utils.py": UTILS_TESTS
                + "\n\ndef test_parse_empty():\n    assert True\n"
            },
        },
    }
    for number in range(114, 124):
        pulls[number] = {
            "title": f"モジュール {number} を足す",
            "files": {f"src/app/module_{number}.py": f"NUMBER = {number}\n"},
        }
    shared = {"src/app/shared.py": "VALUE = 2\n"}
    pulls[SAME_HEAD[0]] = {"title": "共有の値を変える", "files": shared}
    pulls[SAME_HEAD[1]] = {"title": "共有の値を変える（別の PR）", "files": shared, "same_as": 124}
    return pulls


# git --------------------------------------------------------------------------------------


def git_env(home):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.com",
        "GIT_COMMITTER_NAME": "synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.com",
        "GIT_AUTHOR_DATE": _DATE,
        "GIT_COMMITTER_DATE": _DATE,
        "LC_ALL": "C",
    }
    return env


def run_git(repository, *arguments):
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=git_env(repository),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(repository, files):
    for path, text in files.items():
        target = Path(repository) / path
        if text is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _commit(repository, message):
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "-q", "--allow-empty", "-m", message)
    return run_git(repository, "rev-parse", "HEAD")


@dataclass
class Scenario:
    source: Path
    fixture: Path
    manifest: Path
    main_sha: str
    heads: dict = field(default_factory=dict)
    titles: dict = field(default_factory=dict)


def build(directory, base_files, pulls, missing=(), workspace=True):
    """A repository whose `main` holds `base_files` (and, with `workspace`, the spec workspace of
    `spec_scenarios`), one `refs/pull/<n>/head` per PR, and the fixture for the PR list.

    `pulls` maps a number to {"title", "files": {path: text or None}, "same_as"?}. Numbers in
    `missing` are listed by the fixture but have no ref (a deleted fork).
    """
    directory = Path(directory)
    source = directory / "source"
    source.mkdir(parents=True)
    run_git(source, "init", "-q", "-b", DEFAULT_BRANCH)
    manifest = write_workspace(source, absent=False) if workspace else source / "manifest.json"
    _write(source, base_files)
    main_sha = _commit(source, "synthetic default branch")
    scenario = Scenario(source, directory / "fixture", manifest, main_sha)
    for number, pull in sorted(pulls.items()):
        scenario.titles[number] = pull["title"]
        if "same_as" in pull:
            head = scenario.heads[pull["same_as"]]
        else:
            run_git(source, "checkout", "-q", "-B", f"pr-{number}", DEFAULT_BRANCH)
            _write(source, pull["files"])
            head = _commit(source, pull["title"])
            run_git(source, "checkout", "-q", DEFAULT_BRANCH)
        run_git(source, "update-ref", f"refs/pull/{number}/head", head)
        scenario.heads[number] = head
    for number in missing:
        scenario.titles[number] = f"削除された fork からの PR {number}"
        scenario.heads[number] = "f" * 40
    write_pulls_fixture(scenario, missing)
    return scenario


def advance_pull(source, number, files=None):
    """Put one more commit on PR `number`'s head and move `refs/pull/<n>/head` to it. Returns the
    new SHA. The fixture keeps the old head: the PR is now stale."""
    source = Path(source)
    head = run_git(source, "rev-parse", f"refs/pull/{number}/head")
    run_git(source, "checkout", "-q", "--detach", head)
    _write(source, files or {f"src/app/followup_{number}.py": "FOLLOWUP = True\n"})
    new = _commit(source, f"follow-up on {number}")
    run_git(source, "checkout", "-q", DEFAULT_BRANCH)
    run_git(source, "update-ref", f"refs/pull/{number}/head", new)
    return new


def pull_item(number, title, head, main_sha, head_repo=True):
    owner_repo = {"full_name": REPO, "default_branch": DEFAULT_BRANCH}
    return {
        "number": number,
        "title": title,
        "state": "open",
        "head": {"sha": head, "ref": f"pr-{number}", "repo": owner_repo if head_repo else None},
        "base": {"ref": DEFAULT_BRANCH, "sha": main_sha, "repo": owner_repo},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }


def write_pulls_fixture(scenario, missing=()):
    """`pulls?state=open` in one page, in `gh api -i` form."""
    directory = Path(scenario.fixture)
    directory.mkdir(parents=True, exist_ok=True)
    owner, name = REPO.split("/")
    items = [
        pull_item(n, scenario.titles[n], scenario.heads[n], scenario.main_sha, n not in missing)
        for n in sorted(scenario.heads)
    ]
    key = transport.rest_key(
        "pulls", {"owner": owner, "repo": name}, {"state": "open", "per_page": 100, "page": 1}
    )
    (directory / "s0001.http").write_text(_http(200, items), encoding="utf-8")
    (directory / transport.INDEX).write_text(json.dumps({"responses": {key: ["s0001.http"]}}))
    return directory


def build_parallel(directory, numbers=ALL_PRS, missing=()):
    """The 25-PR scenario, or the PRs of it listed in `numbers`."""
    from .spec_scenarios import REQUIREMENTS

    changes = parallel_changes(REQUIREMENTS)
    chosen = {n: changes[n] for n in numbers}
    for pull in chosen.values():
        if pull.get("same_as") not in (None, *chosen):
            pull.pop("same_as")
    return build(directory, BASE_FILES, chosen, missing)


def advance_branch(source, files):
    """Put one more commit on `main` of the source repository. Returns the new SHA."""
    source = Path(source)
    _write(source, files)
    return _commit(source, "follow-up on main")


# Patches ----------------------------------------------------------------------------------

DANGER_PR = 130
DANGER_PATH = ".github/workflows/notes.md"
NOTE_PATH = "docs/design/retention-note.md"
PATCH_PRS = (SUPERSEDED_PR, DANGER_PR)
RETENTION_NOTE = """\
# 保持の補足

## 保持の補足（REQ-0001）

- retention_days: 365

運用の補足です。
"""


def build_patches(directory):
    """The patch scenario. On `main`, `docs/design/retention-note.md` and `infra/logs.tf` state
    REQ-0001's `retention_days` as 365 while the registry says 400 (a `contradiction`: the
    document gets a patch, the IaC file is refused), and `docs/requirements.md` ties REQ-0004 to
    the superseded ADR-0002 (a patch to ADR-0005). PR 107 adds a design document that follows
    ADR-0002 (a patch on its head). PR 130 adds the same text under `.github/workflows/`, which
    is never patched: its fix is refused and the reason recorded."""
    from .spec_scenarios import REQUIREMENTS

    base = dict(BASE_FILES)
    base[NOTE_PATH] = RETENTION_NOTE
    base["infra/logs.tf"] = _replace(
        "infra/logs.tf",
        f"retention_days = {RETENTION_DAYS} # REQ-0001",
        "retention_days = 365 # REQ-0001",
    )
    changes = parallel_changes(REQUIREMENTS)
    pulls = {
        SUPERSEDED_PR: changes[SUPERSEDED_PR],
        DANGER_PR: {
            "title": "CI の補足を足す",
            "files": {DANGER_PATH: changes[SUPERSEDED_PR]["files"]["docs/design/http-retry.md"]},
        },
    }
    return build(directory, base, pulls)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("out_dir")
    args = parser.parse_args()
    scenario = build_parallel(Path(args.out_dir))
    print(json.dumps({"source": str(scenario.source), "fixture": str(scenario.fixture)}))


if __name__ == "__main__":
    main()
