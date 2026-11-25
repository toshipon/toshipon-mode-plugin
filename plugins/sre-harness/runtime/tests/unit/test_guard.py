import os
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace

import pytest

from sre_harness import guard
from sre_harness.errors import BoundaryError, InputError, QualityGateError

SHA = "a" * 40
OTHER = "b" * 40
REPO = {"owner": "example-org", "repo": "demo"}
TOKEN = "synthetic-token-value"


@pytest.fixture
def calls(monkeypatch):
    """Record what would have been started instead of starting it."""
    seen = []

    def fake_run(argv, **options):
        seen.append(SimpleNamespace(argv=argv, **options))
        return SimpleNamespace(returncode=0, stdout="HTTP/2.0 200 OK\r\n\r\n[]", stderr="noise")

    monkeypatch.setattr(guard, "_run", fake_run)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    return seen


ALLOWED_REST = [
    ("rate_limit", {}, {}, "rate_limit"),
    (
        "pulls",
        REPO,
        {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100, "page": 2},
        "repos/example-org/demo/pulls?direction=desc&page=2&per_page=100&sort=updated&state=all",
    ),
    ("pull", {**REPO, "number": 7}, {}, "repos/example-org/demo/pulls/7"),
    ("pull_reviews", {**REPO, "number": 7}, {}, "repos/example-org/demo/pulls/7/reviews"),
    (
        "pull_review_comments",
        {**REPO, "number": 7},
        {"per_page": 50},
        "repos/example-org/demo/pulls/7/comments?per_page=50",
    ),
    ("pull_commits", {**REPO, "number": 7}, {}, "repos/example-org/demo/pulls/7/commits"),
    ("pull_files", {**REPO, "number": 7}, {}, "repos/example-org/demo/pulls/7/files"),
    ("issue_comments", {**REPO, "number": 7}, {}, "repos/example-org/demo/issues/7/comments"),
    ("commit", {**REPO, "sha": SHA}, {}, f"repos/example-org/demo/commits/{SHA}"),
    (
        "compare",
        {**REPO, "base": SHA, "head": OTHER},
        {},
        f"repos/example-org/demo/compare/{SHA}...{OTHER}",
    ),
]

REFUSED_REST = [
    ("merge", {**REPO, "number": 7}, {}),  # not a listed endpoint
    ("repos/example-org/demo/pulls/7/merge", {}, {}),  # a raw path is not an endpoint name
    ("pull", {**REPO, "number": "7/merge"}, {}),
    ("pull", {**REPO, "number": "-1"}, {}),
    ("pull", {**REPO, "number": True}, {}),
    ("pull", {"owner": "example-org", "repo": "..", "number": 7}, {}),
    ("pull", {"owner": "example-org/x", "repo": "demo", "number": 7}, {}),
    ("pull", {"owner": "-X", "repo": "demo", "number": 7}, {}),
    ("pull", REPO, {}),  # a missing value
    ("pull", {**REPO, "number": 7, "extra": 1}, {}),
    ("commit", {**REPO, "sha": "main"}, {}),
    ("compare", {**REPO, "base": SHA, "head": "main"}, {}),
    ("pulls", REPO, {"state": "open&page=9"}),
    ("pulls", REPO, {"method": "POST"}),
    ("pulls", REPO, {"per_page": 101}),
    ("pulls", REPO, {"since": "yesterday"}),
]


@pytest.mark.parametrize("endpoint, values, query, expected", ALLOWED_REST)
def test_listed_rest_reads_are_built_from_templates(endpoint, values, query, expected):
    assert guard.rest_path(endpoint, values, query) == expected


@pytest.mark.parametrize("endpoint, values, query", REFUSED_REST)
def test_everything_else_is_refused_before_a_process_starts(calls, endpoint, values, query):
    with pytest.raises(BoundaryError) as refused:
        guard.gh_rest(endpoint, values, query, token=TOKEN)
    assert refused.value.exit_code == 3
    assert calls == []


def test_every_listed_endpoint_has_an_allowed_example():
    assert {row[0] for row in ALLOWED_REST} == set(guard.REST_ENDPOINTS)


def test_rest_is_a_get_with_a_fixed_argv(calls):
    guard.gh_rest("pull", {**REPO, "number": 7}, token=TOKEN, env={"PATH": "/usr/bin"})
    (call,) = calls
    assert call.argv == [
        "/usr/bin/gh",
        "api",
        "--method",
        "GET",
        "-i",
        "-H",
        "Accept: application/vnd.github+json",
        "repos/example-org/demo/pulls/7",
    ]
    assert TOKEN not in " ".join(call.argv)


def test_gh_never_sees_the_users_own_login(calls, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ambient")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ambient")
    monkeypatch.setenv("GH_HOST", "ambient.invalid")
    guard.gh_rest("rate_limit", token=TOKEN)
    (call,) = calls
    assert call.env["GH_TOKEN"] == TOKEN
    assert call.env["GH_PROMPT_DISABLED"] == "1"
    assert call.env["HOME"] == call.env["GH_CONFIG_DIR"] == call.cwd
    assert not os.path.exists(call.env["GH_CONFIG_DIR"])  # a throwaway, empty directory
    assert "ambient" not in " ".join(call.env.values())
    assert set(call.env) <= {
        *guard._BASE_ENV,
        "GH_TOKEN",
        "GH_CONFIG_DIR",
        "HOME",
        "GH_PROMPT_DISABLED",
        "GH_NO_UPDATE_NOTIFIER",
        "NO_COLOR",
    }


def test_stderr_is_not_returned(calls):
    assert guard.gh_rest("rate_limit", token=TOKEN) == (0, "HTTP/2.0 200 OK\r\n\r\n[]")


@pytest.mark.parametrize("token", ["", "   ", None])
def test_without_a_token_nothing_is_sent(calls, token):
    with pytest.raises(InputError) as stopped:
        guard.gh_rest("rate_limit", token=token)
    assert stopped.value.exit_code == 2
    assert calls == []


def test_the_token_is_read_from_the_named_variable_only():
    assert guard.read_token("REVIEW_TOKEN", {"REVIEW_TOKEN": " abc "}) == "abc"
    for env in ({}, {"REVIEW_TOKEN": ""}, {"REVIEW_TOKEN": "  ", "GH_TOKEN": "ambient"}):
        with pytest.raises(InputError, match="nothing was sent"):
            guard.read_token("REVIEW_TOKEN", env)
    for name in ("", "lower", "A B", "A=1", None):
        with pytest.raises(InputError):
            guard.read_token(name, {})


def test_a_host_is_passed_only_when_named(calls):
    guard.gh_rest("rate_limit", token=TOKEN, host="github.example.com")
    assert calls[0].argv[-2:] == ["--hostname", "github.example.com"]
    with pytest.raises(BoundaryError):
        guard.gh_rest("rate_limit", token=TOKEN, host="--hostname=evil")


@pytest.fixture
def queries(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "QUERY_DIR", tmp_path)
    (tmp_path / "threads.graphql").write_text(
        "# review threads; resolving is a mutation we never send\n"
        "query($owner: String!, $name: String!, $number: Int!, $cursor: String) {\n"
        "  repository(owner: $owner, name: $name) { id }\n}\n"
    )
    (tmp_path / "resolve.graphql").write_text(
        "mutation { resolveReviewThread { clientMutationId } }"
    )
    (tmp_path / "sneaky.graphql").write_text("query { viewer { login } } mutation { x }")
    return tmp_path


def test_graphql_posts_a_bundled_query_with_variables(calls, queries):
    guard.gh_graphql(
        "threads",
        {"owner": "example-org", "name": "demo", "number": 7, "cursor": "Y3Vyc29yOjEw"},
        token=TOKEN,
    )
    argv = calls[0].argv
    assert argv[:4] == ["/usr/bin/gh", "api", "graphql", "-i"]  # -i: status line and headers
    assert "--method" not in argv  # GraphQL must be a POST; GET returns the whole schema
    assert argv[4:12] == [
        "-f",
        "cursor=Y3Vyc29yOjEw",
        "-f",
        "name=demo",
        "-F",
        "number=7",
        "-f",
        "owner=example-org",
    ]
    assert argv[12] == "-f" and argv[13].startswith("query=# review threads")


@pytest.mark.parametrize("name", ["resolve", "sneaky", "missing", "../threads", "Threads", ""])
def test_graphql_refuses_anything_but_a_bundled_query(calls, queries, name):
    with pytest.raises(BoundaryError):
        guard.gh_graphql(name, token=TOKEN)
    assert calls == []


@pytest.mark.parametrize(
    "variables",
    [
        {"query": "mutation { x }"},
        {"cursor": "@/etc/passwd"},
        {"cursor": "a b"},
        {"owner": "a/b"},
        {"number": -1},
        {"number": True},
        {"cursor": None},
        {"bad-name": "x"},
    ],
)
def test_graphql_variables_are_checked(calls, queries, variables):
    with pytest.raises(BoundaryError):
        guard.gh_graphql("threads", variables, token=TOKEN)
    assert calls == []


def test_bundled_queries_are_all_read_only():
    for path in guard.QUERY_DIR.glob("*.graphql"):
        guard.graphql_query(path.stem)


def allowed_git(tmp_path):
    patch = tmp_path / "PT-0001.diff"
    patch.write_text("")
    return [
        ["init", "--bare", "--quiet"],
        [
            "fetch",
            "--no-tags",
            "--quiet",
            "https://github.com/example-org/demo.git",
            "+refs/heads/main:refs/sre-harness/base/main",
            "+refs/pull/7/head:refs/sre-harness/pull/7",
        ],
        ["fetch", "--quiet", str(tmp_path), "+refs/heads/main:refs/sre-harness/base/main"],
        ["cat-file", "-e", SHA],
        ["cat-file", "blob", f"{SHA}:docs/requirements.md"],
        ["show", "--no-patch", "--format=%H%n%P%n%cI", SHA],
        ["diff", "--no-ext-diff", "--no-textconv", "--unified=3", SHA, OTHER],
        ["diff", "--no-ext-diff", "--name-status", SHA, OTHER, "--", "docs/requirements.md"],
        ["merge-base", SHA, OTHER],
        ["rev-parse", "--verify", "--quiet", "refs/sre-harness/pull/7"],
        ["read-tree", SHA],
        ["apply", "--cached", "--check", str(patch)],
    ]


REFUSED_GIT = [
    [],
    ["push", "origin", "main"],
    ["merge", SHA],
    ["commit", "-m", "x"],
    ["worktree", "add", "/tmp/x", SHA],
    ["checkout", SHA],
    ["reset", "--hard", SHA],
    ["update-ref", "refs/heads/main", SHA],
    ["config", "user.name", "x"],
    ["clone", "https://github.com/example-org/demo.git"],
    ["remote", "add", "origin", "https://github.com/example-org/demo.git"],
    ["-c", "core.sshCommand=x", "fetch"],
    ["init"],  # a non-bare repository has a work tree
    ["apply", "PATCH"],  # would write to a work tree
    ["apply", "--cached", "PATCH"],  # would write to the index
    ["apply", "--check", "PATCH"],
    ["apply", "--cached", "--check", "relative.diff"],
    ["fetch", "https://github.com/example-org/demo.git"],  # no refspec: would use defaults
    [
        "fetch",
        "--upload-pack=touch x",
        "https://github.com/example-org/demo.git",
        "+refs/heads/a:refs/sre-harness/a",
    ],
    ["fetch", "ext::sh -c x", "+refs/heads/a:refs/sre-harness/a"],
    ["fetch", "git@github.com:example-org/demo.git", "+refs/heads/a:refs/sre-harness/a"],
    ["fetch", "https://token@github.com/example-org/demo.git", "+refs/heads/a:refs/sre-harness/a"],
    ["fetch", "https://github.com/example-org/demo.git", "+refs/heads/a:refs/heads/a"],
    ["fetch", "https://github.com/example-org/demo.git", "refs/heads/a"],
    ["diff", "--output=/tmp/x", SHA, OTHER],
    ["diff", SHA, OTHER, "--", "../outside"],
    ["diff", SHA, OTHER, "--", "--output=/tmp/x"],
    ["show", f"{SHA}:../x"],
    ["show", "--textconv", SHA],
    ["cat-file", "blob", "HEAD:secret"],
    ["rev-parse", "HEAD"],
    ["rev-parse", "--git-dir"],
    ["read-tree", "-u", SHA],
    ["merge-base", "--fork-point", SHA],
]


def test_listed_git_reads_pass(tmp_path):
    examples = allowed_git(tmp_path)
    for arguments in examples:
        assert guard.check_git(arguments) == arguments
    assert {arguments[0] for arguments in examples} == set(guard.GIT_SUBCOMMANDS)


@pytest.mark.parametrize(
    "arguments", REFUSED_GIT, ids=lambda arguments: " ".join(arguments) or "empty"
)
def test_git_writes_are_refused_before_a_process_starts(calls, tmp_path, arguments):
    with pytest.raises(BoundaryError) as refused:
        guard.git(tmp_path, arguments, token=TOKEN)
    assert refused.value.exit_code == 3
    assert calls == []


def test_git_ignores_the_users_config_and_credential_helper(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "ambient")
    remote = "https://github.com/example-org/demo.git"
    refspec = "+refs/pull/7/head:refs/sre-harness/pull/7"
    guard.git(
        tmp_path, ["fetch", "--quiet", remote, refspec], token=TOKEN, index_file=tmp_path / "i"
    )
    (call,) = calls
    env = call.env
    assert call.argv == ["/usr/bin/git", "fetch", "--quiet", remote, refspec]
    assert TOKEN not in " ".join(call.argv)
    assert env["GIT_DIR"] == str(tmp_path)
    assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_INDEX_FILE"] == str(tmp_path / "i")
    assert "GIT_SSH_COMMAND" not in env and "HOME" not in env
    config = {
        env[f"GIT_CONFIG_KEY_{number}"]: env[f"GIT_CONFIG_VALUE_{number}"]
        for number in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert config["credential.helper"] == ""
    assert config["protocol.ext.allow"] == "never"
    assert config["http.https://github.com/.extraheader"].startswith("Authorization: Basic ")
    assert TOKEN not in config["http.https://github.com/.extraheader"]


def test_the_token_is_not_offered_to_a_local_source(calls, tmp_path):
    guard.git(
        tmp_path,
        ["fetch", str(tmp_path), "+refs/heads/main:refs/sre-harness/base/main"],
        token=TOKEN,
    )
    assert not any("extraheader" in value for value in calls[0].env.values())


def test_the_repository_path_must_be_absolute(calls):
    with pytest.raises(BoundaryError):
        guard.git("relative", ["rev-parse", "--verify", SHA])
    assert calls == []


# Claude CLI -------------------------------------------------------------------------------

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:sre-harness:example",
    "type": "object",
}
KEY_VALUE = "synthetic-anthropic-credential"


@pytest.fixture
def claude_calls(monkeypatch):
    """Record the claude process instead of starting it, and what its cwd held at that time."""
    seen = []

    def fake_run(argv, **options):
        seen.append(SimpleNamespace(argv=argv, listing=os.listdir(options["cwd"]), **options))
        return SimpleNamespace(returncode=0, stdout='{"is_error": false}', stderr="secret noise")

    monkeypatch.setattr(guard, "_run", fake_run)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    return seen


def isolated_argv(flag, budget):
    return [
        "/usr/bin/claude",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        '{"type": "object"}',
        flag,
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-session-persistence",
        "--max-budget-usd",
        budget,
    ]


ALLOWED_CLAUDE = [
    ({"auth_mode": "subscription", "max_budget_usd": 0.5}, isolated_argv("--safe-mode", "0.5000")),
    ({"auth_mode": "api-key", "max_budget_usd": 2}, isolated_argv("--bare", "2.0000")),
    (
        {"auth_mode": "subscription", "max_budget_usd": 0.25, "model": "haiku"},
        isolated_argv("--safe-mode", "0.2500") + ["--model", "haiku"],
    ),
    (
        {"auth_mode": "subscription", "max_budget_usd": 1, "model": "claude-sonnet-4-5"},
        isolated_argv("--safe-mode", "1.0000") + ["--model", "claude-sonnet-4-5"],
    ),
]

REFUSED_CLAUDE = [
    {"auth_mode": "bypass", "max_budget_usd": 1},
    {"auth_mode": None, "max_budget_usd": 1},
    {"auth_mode": "subscription", "max_budget_usd": 0},
    {"auth_mode": "subscription", "max_budget_usd": -1},
    {"auth_mode": "subscription", "max_budget_usd": True},
    {"auth_mode": "subscription", "max_budget_usd": "1"},
    {"auth_mode": "subscription", "max_budget_usd": float("nan")},
    {"auth_mode": "subscription", "max_budget_usd": 10_000},
    {"auth_mode": "subscription", "max_budget_usd": 1, "timeout": 5},
    {"auth_mode": "subscription", "max_budget_usd": 1, "timeout": True},
    {"auth_mode": "subscription", "max_budget_usd": 1, "model": "--dangerously-skip-permissions"},
    {"auth_mode": "subscription", "max_budget_usd": 1, "model": "haiku; echo x"},
    {"auth_mode": "subscription", "max_budget_usd": 1, "model": ""},
]


def claude_env():
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/home/someone",
        "ANTHROPIC_API_KEY": KEY_VALUE,
        "GH_TOKEN": TOKEN,
        "AWS_PROFILE": "ambient",
        "CLAUDE_CONFIG_DIR": "/home/someone/.claude",
    }


@pytest.mark.parametrize("options, argv", ALLOWED_CLAUDE)
def test_claude_runs_isolated_with_a_fixed_argv(claude_calls, options, argv):
    code, stdout = guard.claude("PROMPT", SCHEMA, timeout=60, env=claude_env(), **options)
    assert (code, stdout) == (0, '{"is_error": false}')
    (call,) = claude_calls
    assert call.argv == argv
    assert call.input == "PROMPT"  # the prompt goes through stdin, never argv
    assert call.listing == []  # cwd is a new, empty directory
    assert not Path(call.cwd).exists()  # and it is gone afterwards
    assert call.timeout == 60 and call.check is False and call.text is True
    assert not any("dangerously" in part or "permission" in part for part in call.argv)


def test_subscription_keeps_home_and_drops_the_api_key(claude_calls):
    guard.claude(
        "P", SCHEMA, auth_mode="subscription", timeout=60, max_budget_usd=1, env=claude_env()
    )
    env = claude_calls[0].env
    assert env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/home/someone"}


def test_api_key_mode_passes_only_the_key_and_an_empty_home(claude_calls):
    guard.claude("P", SCHEMA, auth_mode="api-key", timeout=60, max_budget_usd=1, env=claude_env())
    call = claude_calls[0]
    assert call.env["ANTHROPIC_API_KEY"] == KEY_VALUE
    assert call.env["HOME"] == call.cwd
    assert set(call.env) == {"PATH", "LANG", "HOME", "ANTHROPIC_API_KEY"}
    assert KEY_VALUE not in " ".join(call.argv)


@pytest.mark.parametrize(
    "mode, missing", [("api-key", "ANTHROPIC_API_KEY"), ("subscription", "HOME")]
)
def test_missing_credentials_stop_before_a_process_starts(claude_calls, mode, missing):
    env = claude_env()
    del env[missing]
    with pytest.raises(InputError) as stopped:
        guard.claude("P", SCHEMA, auth_mode=mode, timeout=60, max_budget_usd=1, env=env)
    assert claude_calls == []
    assert KEY_VALUE not in str(stopped.value)


@pytest.mark.parametrize("options", REFUSED_CLAUDE)
def test_claude_refuses_values_outside_the_table(claude_calls, options):
    options = {"timeout": 60, **options}
    with pytest.raises(BoundaryError):
        guard.claude("P", SCHEMA, env=claude_env(), **options)
    assert claude_calls == []


def test_claude_stderr_is_not_returned_and_a_timeout_is_a_quality_failure(monkeypatch):
    def slow(argv, **options):
        raise TimeoutExpired(argv, options["timeout"], stderr="secret noise")

    monkeypatch.setattr(guard, "_run", slow)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    with pytest.raises(QualityGateError) as stopped:
        guard.claude(
            "P", SCHEMA, auth_mode="subscription", timeout=60, max_budget_usd=1, env=claude_env()
        )
    assert "secret noise" not in str(stopped.value)
