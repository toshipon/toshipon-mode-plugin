"""The only module that starts processes or could reach the network.

Everything it can run is listed below as data. A request that is not in a table is refused
with exit code 3 before any process starts. The tables are checked by table-driven tests,
and an AST test keeps process and network imports out of every other module.
"""

import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import BoundaryError, InputError, QualityGateError

QUERY_DIR = Path(__file__).resolve().parent / "github" / "queries"
REGEX_WORKER = Path(__file__).resolve().parent / "regex_worker.py"

_run = subprocess.run  # tests replace this; nothing else in the package may start a process

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}")
_NUMBER = re.compile(r"[1-9][0-9]{0,8}")
_SHA = re.compile(r"[0-9a-f]{40}")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_QUERY_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VARIABLE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_CURSOR = re.compile(r"[A-Za-z0-9+/=_:.-]{1,400}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
# One path element of a ref: no leading or trailing dot, so `.hidden`, `a.` and `//` fail.
_REF_PART = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?")
_OWN_REF = re.compile(r"refs/sre-harness/([^:]{1,200})")
_REFSPEC = re.compile(r"\+?refs/(?:heads|pull)/([^:]{1,200}):refs/sre-harness/([^:]{1,200})")
_REMOTE_URL = re.compile(r"https://([a-z0-9.-]+)/[A-Za-z0-9-]+/[A-Za-z0-9._-]+\.git")
DEFAULT_GIT_HOST = "github.com"

# REST: GET only, and only these paths. Values are checked against the pattern named here.
REST_ENDPOINTS = {
    "rate_limit": "rate_limit",
    "pulls": "repos/{owner}/{repo}/pulls",
    "pull": "repos/{owner}/{repo}/pulls/{number}",
    "pull_reviews": "repos/{owner}/{repo}/pulls/{number}/reviews",
    "pull_review_comments": "repos/{owner}/{repo}/pulls/{number}/comments",
    "pull_commits": "repos/{owner}/{repo}/pulls/{number}/commits",
    "pull_files": "repos/{owner}/{repo}/pulls/{number}/files",
    "issue_comments": "repos/{owner}/{repo}/issues/{number}/comments",
    "commit": "repos/{owner}/{repo}/commits/{sha}",
    "compare": "repos/{owner}/{repo}/compare/{base}...{head}",
}
_PATH_VALUES = {
    "owner": _OWNER,
    "repo": _REPO,
    "number": _NUMBER,
    "sha": _SHA,
    "base": _SHA,
    "head": _SHA,
}
REST_QUERY = {
    "state": re.compile(r"all|open|closed"),
    "sort": re.compile(r"created|updated"),
    "direction": re.compile(r"asc|desc"),
    "per_page": re.compile(r"[1-9][0-9]?|100"),
    "page": re.compile(r"[1-9][0-9]{0,5}"),
    "since": _ISO_TIME,
}

# git: the harness reads objects from its own bare repository. No work tree, no writes to a
# remote. Each subcommand lists the flags it may carry; any other flag is refused.
GIT_SUBCOMMANDS = {
    "init": {"--bare", "--quiet"},
    "fetch": {"--no-tags", "--quiet", "--prune", "--no-write-fetch-head"},
    "cat-file": {"-e", "-t", "-s", "-p", "blob", "commit", "tree"},
    "show": {"--no-patch", "--format=%H%n%P%n%cI", "--no-ext-diff", "--no-textconv"},
    "diff": {
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--name-status",
        "--unified=3",
        "--unified=0",
        "--no-renames",
        "--binary",
        "--cached",
    },
    "merge-base": set(),
    "rev-parse": {"--verify", "--quiet"},
    "read-tree": set(),
    "apply": {"--cached", "--check"},
}
_GIT_REQUIRED = {"init": ("--bare",), "apply": ("--cached", "--check")}
# These write an index. Without a temporary one (GIT_INDEX_FILE) they would write the bare
# repository's own index, so `git` refuses them when no `index_file` is given.
_GIT_NEEDS_INDEX = frozenset({"read-tree", "apply"})
_GIT_CONFIG = (
    ("credential.helper", ""),
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("gc.auto", "0"),
    ("protocol.ext.allow", "never"),
    ("fetch.fsckObjects", "true"),
)

_BASE_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")

# Claude CLI: print mode, structured output, no tools, no MCP servers, no slash commands, no
# saved session, a spend cap per call. Permission-bypass flags are never added.
CLAUDE_AUTH = {"api-key": "--bare", "subscription": "--safe-mode"}
CLAUDE_MAX_BUDGET_USD = 100.0
CLAUDE_TIMEOUT = (30, 3600)
_CLAUDE_MODEL = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")

# Regular expressions written by a backend: matched by the bundled worker in its own Python
# process (`-I`: no user site, no PYTHON* variables, no script directory on sys.path), killed at
# the time limit. argv is fixed; patterns and texts travel on stdin.
REGEX_TIMEOUT = (0.1, 60.0)
REGEX_MAX_PATTERNS = 8
REGEX_MAX_PATTERN_LENGTH = 200
REGEX_MAX_INPUT_BYTES = 16 * 1024 * 1024


class RegexTimeout(QualityGateError):
    reason = "regex_timeout"


def read_token(env_name, env=None):
    """The token comes from the environment variable the caller named. Empty means stop."""
    env = os.environ if env is None else env
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise InputError("--auth-env must name an environment variable (A-Z, 0-9, _).")
    token = env.get(env_name, "")
    if not token.strip():
        raise InputError(
            f"environment variable {env_name} is empty; nothing was sent. "
            "Export a read-only token there and pass its name with --auth-env."
        )
    return token.strip()


def _checked(pattern, value, what):
    text = str(value)
    if isinstance(value, bool) or not pattern.fullmatch(text):
        raise BoundaryError(f"refused {what}: the value is not in the allowed form.")
    return text


def _base_env(env):
    env = os.environ if env is None else env
    return {name: env[name] for name in _BASE_ENV if name in env}


def _executable(name):
    path = shutil.which(name)
    if not path:
        raise InputError(f"{name} is not installed or not on PATH.")
    return path


def rest_path(endpoint, values=None, query=None):
    """Build the request path from a template. Nothing outside the tables gets through."""
    if endpoint not in REST_ENDPOINTS:
        raise BoundaryError(f"refused REST endpoint {endpoint!r}: not in the read-only allowlist.")
    values = dict(values or {})
    template = REST_ENDPOINTS[endpoint]
    names = set(re.findall(r"{(\w+)}", template))
    if set(values) != names:
        raise BoundaryError(
            f"refused REST endpoint {endpoint!r}: it takes exactly {sorted(names)}."
        )
    path = template.format(
        **{name: _checked(_PATH_VALUES[name], values[name], name) for name in names}
    )
    pairs = []
    for key, value in sorted((query or {}).items()):
        if key not in REST_QUERY:
            raise BoundaryError(f"refused REST query parameter {key!r}.")
        pairs.append(f"{key}={_checked(REST_QUERY[key], value, key)}")
    return path + ("?" + "&".join(pairs) if pairs else "")


def _gh_env(token, config_dir, env):
    child = _base_env(env)
    # An empty config directory keeps gh away from the user's own login (~/.config/gh).
    child.update(
        GH_TOKEN=token,
        GH_CONFIG_DIR=config_dir,
        HOME=config_dir,
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        NO_COLOR="1",
    )
    return child


def _require_token(token):
    if not isinstance(token, str) or not token.strip():
        raise InputError("no token was given; nothing was sent.")


def _gh(arguments, token, host, env, timeout):
    _require_token(token)
    argv = [_executable("gh"), "api", *arguments]
    if host is not None:
        argv += ["--hostname", _checked(_HOST, host, "host")]
    with tempfile.TemporaryDirectory(prefix="sre-harness-gh-") as config_dir:
        result = _run(
            argv,
            env=_gh_env(token, config_dir, env),
            cwd=config_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    # stderr may quote the request or the token source; it is never returned or stored.
    return result.returncode, result.stdout


def gh_rest(endpoint, values=None, query=None, *, token, host=None, env=None, timeout=60):
    """One GET, one page. Returns (exit code, raw stdout with the status line and headers)."""
    path = rest_path(endpoint, values, query)
    accept = "Accept: application/vnd.github+json"
    return _gh(["--method", "GET", "-i", "-H", accept, path], token, host, env, timeout)


def graphql_query(name):
    """Text of a bundled query. Queries are files in this package, never built from input."""
    path = QUERY_DIR / f"{_checked(_QUERY_NAME, name, 'query name')}.graphql"
    if not path.is_file():
        raise BoundaryError(f"refused GraphQL query {name!r}: it is not bundled.")
    text = path.read_text(encoding="utf-8")
    code = re.sub(r"#[^\n]*", "", text)
    if not re.match(r"\s*query\b", code) or re.search(r"\b(?:mutation|subscription)\b", code):
        raise BoundaryError(f"refused GraphQL query {name!r}: only query operations are allowed.")
    return text


def graphql_fields(variables):
    """Variables as gh arguments. Strings go through -f, which gh never reinterprets."""
    arguments = []
    for key, value in sorted((variables or {}).items()):
        _checked(_VARIABLE_NAME, key, "GraphQL variable name")
        if key == "query":
            raise BoundaryError("refused GraphQL variable 'query': the query text is bundled.")
        if isinstance(value, bool) or value is None:
            raise BoundaryError(f"refused GraphQL variable {key!r}: unsupported type.")
        if isinstance(value, int):
            arguments += ["-F", f"{key}={_checked(_NUMBER, value, key)}"]
        elif key in {"owner", "name"}:
            pattern = _OWNER if key == "owner" else _REPO
            arguments += ["-f", f"{key}={_checked(pattern, value, key)}"]
        else:
            arguments += ["-f", f"{key}={_checked(_CURSOR, value, key)}"]
    return arguments


def gh_graphql(name, variables=None, *, token, host=None, env=None, timeout=60):
    """POST one bundled query. Returns (exit code, raw stdout with the status line and headers)."""
    query = graphql_query(name)
    arguments = ["graphql", "-i", *graphql_fields(variables), "-f", f"query={query}"]
    return _gh(arguments, token, host, env, timeout)


def _is_ref_name(value):
    """The part after refs/<kind>/. Refuses `..`, `@{`, empty elements and `.lock` endings."""
    return all(
        _REF_PART.fullmatch(part) and ".." not in part and not part.endswith(".lock")
        for part in value.split("/")
    )


def _ref_allowed(pattern, value):
    match = pattern.fullmatch(value)
    return bool(match) and all(_is_ref_name(name) for name in match.groups())


def _git_value_allowed(subcommand, value, position):
    if subcommand == "init":
        return False
    if subcommand == "fetch":
        if position == 0:
            return bool(_REMOTE_URL.fullmatch(value)) or _is_local_source(value)
        return _ref_allowed(_REFSPEC, value)
    if subcommand == "apply":
        return _is_patch_file(value)
    if subcommand in {"show", "cat-file"} and ":" in value:
        sha, _, path = value.partition(":")
        return bool(_SHA.fullmatch(sha)) and _is_repo_path(path)
    return bool(_SHA.fullmatch(value)) or _ref_allowed(_OWN_REF, value)


def _is_local_source(value):
    path = Path(value)
    return path.is_absolute() and ".." not in path.parts and path.is_dir()


def _is_patch_file(value):
    path = Path(value)
    return path.is_absolute() and ".." not in path.parts and path.is_file()


def _is_repo_path(value):
    parts = value.split("/")
    return bool(value) and not value.startswith("/") and "\0" not in value and ".." not in parts


def check_git(arguments):
    """Refuse anything that is not a listed read-only use of git. Returns the argument list."""
    arguments = [str(argument) for argument in arguments]
    if not arguments or arguments[0] not in GIT_SUBCOMMANDS:
        name = arguments[0] if arguments else ""
        raise BoundaryError(f"refused git {name!r}: not in the read-only allowlist.")
    subcommand, rest = arguments[0], arguments[1:]
    flags = GIT_SUBCOMMANDS[subcommand]
    paths = []
    if "--" in rest:
        if subcommand != "diff":
            raise BoundaryError(f"refused git {subcommand}: path arguments are not allowed.")
        cut = rest.index("--")
        rest, paths = rest[:cut], rest[cut + 1 :]
    position = 0
    for argument in rest:
        if argument in flags:
            continue
        if argument.startswith("-") or not _git_value_allowed(subcommand, argument, position):
            raise BoundaryError(f"refused git {subcommand}: argument {argument!r} is not allowed.")
        position += 1
    for path in paths:
        if path.startswith("-") or not _is_repo_path(path):
            raise BoundaryError(f"refused git {subcommand}: path {path!r} is not allowed.")
    for required in _GIT_REQUIRED.get(subcommand, ()):
        if required not in rest:
            raise BoundaryError(f"refused git {subcommand}: it must carry {required}.")
    return arguments


def _git_env(git_dir, token, index_file, remote, env):
    child = _base_env(env)
    config = list(_GIT_CONFIG)
    if token and remote and _REMOTE_URL.fullmatch(remote):
        # The token travels in the environment, never in argv, the URL or the repository config.
        # git() has already checked that this host is the allowed one.
        host = remote.split("/")[2]
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config.append((f"http.https://{host}/.extraheader", f"Authorization: Basic {basic}"))
    child.update(
        GIT_DIR=str(git_dir),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        GIT_OPTIONAL_LOCKS="0",
        GIT_CONFIG_COUNT=str(len(config)),
    )
    for number, (key, value) in enumerate(config):
        child[f"GIT_CONFIG_KEY_{number}"] = key
        child[f"GIT_CONFIG_VALUE_{number}"] = value
    if index_file is not None:
        child["GIT_INDEX_FILE"] = str(index_file)
    return child


def _check_remote_host(remote, host):
    """An https remote must be on the allowed host (github.com unless the caller names one)."""
    allowed = _checked(_HOST, DEFAULT_GIT_HOST if host is None else host, "host")
    match = _REMOTE_URL.fullmatch(remote)
    if match and match.group(1) != allowed:
        raise BoundaryError(
            f"refused git fetch: the remote is not on {allowed}; pass --host to read another host."
        )


def git(git_dir, arguments, *, token=None, host=None, index_file=None, env=None, timeout=300):
    """Run one allowlisted git command against the harness's own bare repository.

    Returns (exit code, stdout bytes). The user's checkout, config and credential helper are
    never read. An https remote must be on `host` (github.com by default), and only that host
    is given the token. A local directory source never gets the token.
    """
    arguments = check_git(arguments)
    if arguments[0] in _GIT_NEEDS_INDEX and index_file is None:
        raise BoundaryError(f"refused git {arguments[0]}: it runs only against a temporary index.")
    git_dir = Path(git_dir)
    if not git_dir.is_absolute():
        raise BoundaryError("refused git: the repository path must be absolute.")
    remote = None
    if arguments[0] == "fetch":
        values = [argument for argument in arguments[1:] if not argument.startswith("-")]
        if len(values) < 2:
            raise BoundaryError(
                "refused git fetch: a source and at least one refspec are required."
            )
        remote = values[0]
        _check_remote_host(remote, host)
    result = _run(
        [_executable("git"), *arguments],
        env=_git_env(git_dir, token, index_file, remote, env),
        cwd=str(git_dir) if git_dir.is_dir() else str(git_dir.parent),
        capture_output=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode, result.stdout


def _claude_budget(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= CLAUDE_MAX_BUDGET_USD
    ):
        raise BoundaryError(
            f"refused claude budget: it must be above 0 and at most {CLAUDE_MAX_BUDGET_USD} USD."
        )
    return f"{value:.4f}"


def _claude_env(auth_mode, cwd, env):
    env = os.environ if env is None else env
    child = _base_env(env)
    if auth_mode == "api-key":
        if not env.get("ANTHROPIC_API_KEY", "").strip():
            raise InputError("--auth-mode api-key needs ANTHROPIC_API_KEY; nothing was sent.")
        child["ANTHROPIC_API_KEY"] = env["ANTHROPIC_API_KEY"]
        child["HOME"] = cwd
    else:
        # The subscription login lives under the user's home; nothing else is passed through.
        if not env.get("HOME"):
            raise InputError("--auth-mode subscription needs HOME; nothing was sent.")
        child["HOME"] = env["HOME"]
        # The login is stored under the account name (the macOS keychain); the CLI needs it.
        child.update({name: env[name] for name in ("USER", "LOGNAME") if env.get(name)})
    return child


def claude_argv(schema, *, auth_mode, max_budget_usd, model=None):
    """The fixed argv. The schema loses `$schema` and `$id`, which the CLI does not accept."""
    if auth_mode not in CLAUDE_AUTH:
        raise BoundaryError(f"refused claude auth mode {auth_mode!r}: use {sorted(CLAUDE_AUTH)}.")
    transport = {key: value for key, value in schema.items() if key not in {"$schema", "$id"}}
    argv = [
        _executable("claude"),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(transport),
        CLAUDE_AUTH[auth_mode],
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-session-persistence",
        "--max-budget-usd",
        _claude_budget(max_budget_usd),
    ]
    if model is not None:
        argv += ["--model", _checked(_CLAUDE_MODEL, model, "claude model")]
    return argv


def claude(prompt, schema, *, auth_mode, timeout, max_budget_usd, model=None, env=None):
    """One tool-free Claude call. The prompt goes through stdin; cwd is a new empty directory.

    Returns (exit code, stdout). stderr is never returned or stored.
    """
    low, high = CLAUDE_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not low <= timeout <= high:
        raise BoundaryError(f"refused claude timeout: it must be {low}-{high} seconds.")
    if not isinstance(prompt, str):
        raise BoundaryError("refused claude prompt: it must be text.")
    argv = claude_argv(schema, auth_mode=auth_mode, max_budget_usd=max_budget_usd, model=model)
    with tempfile.TemporaryDirectory(prefix="sre-harness-claude-") as cwd:
        child = _claude_env(auth_mode, cwd, env)
        try:
            result = _run(
                argv,
                input=prompt,
                env=child,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise QualityGateError(f"claude did not finish within {timeout} seconds.") from None
    return result.returncode, result.stdout


def regex_search(patterns, texts, *, timeout, env=None):
    """For each pattern, the indexes of the texts `re.search` finds it in.

    `re` cannot be interrupted, so a pattern that backtracks without end would stop the harness.
    The match runs in `regex_worker.py` under `sys.executable -I` and is killed after `timeout`
    seconds (RegexTimeout). An invalid pattern is an InputError naming its index.
    """
    low, high = REGEX_TIMEOUT
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not low <= timeout <= high
    ):
        raise BoundaryError(f"refused regex timeout: it must be {low}-{high} seconds.")
    if (
        not isinstance(patterns, list)
        or not 1 <= len(patterns) <= REGEX_MAX_PATTERNS
        or not all(isinstance(p, str) and 0 < len(p) <= REGEX_MAX_PATTERN_LENGTH for p in patterns)
    ):
        raise BoundaryError(
            f"refused regex patterns: 1-{REGEX_MAX_PATTERNS} strings of at most "
            f"{REGEX_MAX_PATTERN_LENGTH} characters."
        )
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        raise BoundaryError("refused regex texts: they must be a list of strings.")
    payload = json.dumps({"patterns": patterns, "texts": texts}, ensure_ascii=True)
    if len(payload) > REGEX_MAX_INPUT_BYTES:
        raise QualityGateError(
            f"the regex input is over {REGEX_MAX_INPUT_BYTES} bytes; stopped instead of cutting it."
        )
    argv = [sys.executable, "-I", str(REGEX_WORKER)]
    with tempfile.TemporaryDirectory(prefix="sre-harness-regex-") as cwd:
        try:
            result = _run(
                argv,
                input=payload,
                env=_base_env(env),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RegexTimeout(
                f"the pattern match did not finish within {timeout} seconds; it was stopped."
            ) from None
    if result.returncode != 0:
        raise QualityGateError(f"the pattern worker failed with exit code {result.returncode}.")
    try:
        answer = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise QualityGateError("the pattern worker did not answer with JSON.") from None
    if isinstance(answer, dict) and isinstance(answer.get("invalid"), int):
        raise InputError(f"pattern {answer['invalid']} is not a valid regular expression.")
    matches = answer.get("matches") if isinstance(answer, dict) else None
    if (
        not isinstance(matches, list)
        or len(matches) != len(patterns)
        or not all(
            isinstance(found, list)
            and all(
                isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(texts)
                for i in found
            )
            for found in matches
        )
    ):
        raise QualityGateError("the pattern worker answered in an unexpected shape.")
    return matches
