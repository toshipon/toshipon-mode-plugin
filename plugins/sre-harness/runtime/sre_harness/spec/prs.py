"""Open PRs and their content, read from a harness-owned bare repository.

- The PR list (number, title, head SHA, base ref and SHA) comes from the transport
  (`GET repos/{owner}/{repo}/pulls?state=open`, one page at a time).
- The bare repository lives in the state root's `cache/git/`, one per repository and fetch
  source. It is created with `git init --bare` and receives only the default branch and the
  open PRs' `refs/pull/<n>/head`, under `refs/sre-harness/...`. No work tree is made, the
  user's checkout is never touched, and partial clone is not used.
- Every git command goes through `guard.git`, which checks each argument (SHAs are 40 hex
  digits, refs are the harness's own, a local fetch source is an absolute directory).
- File content is read with `git cat-file blob <sha>:<path>` into memory. Nothing of a PR is
  written to disk except the diff text in the run's `diff-cache/`.
- A diff is computed once per (merge base, head SHA): `DiffCache` keeps the text in
  `diff-cache/<sha256>.diff` and counts what it computed and what it reused. Each PR costs a
  fixed number of git commands plus two blob reads per changed file it can read, so the total
  grows linearly with the number of PRs; pairs of PRs are never diffed against each other.
- One diff over `MAX_DIFF_BYTES` raises `DiffTooLarge`: that PR is not analyzed (the stage
  records it as `input_missing` and goes on with the others). A file over `MAX_BLOB_BYTES` or
  a run's diffs over `MAX_RUN_DIFF_BYTES` stop the stage (exit 1) instead of being cut.
- A diff is read as git writes it: lines end at "\n" only, and a hunk's body is consumed by
  the line counts in its `@@` header, so nothing inside a changed line is read as a header.
"""

import codecs
import re

from .. import guard, paths, records
from ..errors import InputError, QualityGateError
from ..github import rest
from .ingest_file import MAX_DOCUMENT_BYTES

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_RUN_DIFF_BYTES = 64 * 1024 * 1024
MAX_BLOB_BYTES = MAX_DOCUMENT_BYTES
PER_PAGE = 100
MAX_PAGES = 30
DIFF_ARGS = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--unified=0")

_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"(?!.*\.\.)(?!/)[A-Za-z0-9][A-Za-z0-9._/-]{0,199}(?<![/.])")
_HUNK = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@.*")
_QUOTED_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"')


def branch_name(value):
    if not isinstance(value, str) or not _BRANCH.fullmatch(value):
        raise InputError(f"not a branch name the harness reads: {value!r}")
    return value


# The PR list -------------------------------------------------------------------------------


def _object(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) else None


def _pull(item):
    """(pull, None) or (partial pull or None, reason) for one item of the list."""
    if not isinstance(item, dict):
        return None, "not_an_object"
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None, "no_number"
    head, base = _object(item.get("head")), _object(item.get("base"))
    sha = _text(head.get("sha"))
    if sha is None or not _SHA.fullmatch(sha):
        return {"number": number}, "no_head_sha"
    base_sha = _text(base.get("sha"))
    pull = {
        "number": number,
        "title": (_text(item.get("title")) or "")[:300],
        "head_sha": sha,
        "head_repo_missing": head.get("repo") is None,
        "base_ref": _text(base.get("ref")),
        "base_sha": base_sha if base_sha and _SHA.fullmatch(base_sha) else None,
        "default_branch": _text(_object(base.get("repo")).get("default_branch")),
    }
    return pull, None


def list_open(transport, limiter, owner, name):
    """(pulls, gaps). A page that cannot be read stops the stage: without the full list the
    analysis would miss conflicts without saying so."""
    pulls, gaps, page = [], [], 1
    values = {"owner": owner, "repo": name}
    while True:
        query = {"state": "open", "per_page": PER_PAGE, "page": page}
        request = guard.rest_path("pulls", values, query)
        response = rest.fetch(limiter, request, lambda: transport.rest("pulls", values, query))
        reason = rest.failure_reason(response)
        if reason is None:
            try:
                data = response.data()
            except ValueError:
                data, reason = None, "not_json"
        if reason is not None or not isinstance(data, list):
            raise QualityGateError(
                f"the open PR list could not be read (page {page}: {reason or 'not_a_list'}); "
                "nothing was analyzed."
            )
        for position, item in enumerate(data, start=1):
            pull, problem = _pull(item)
            if problem is None:
                pulls.append(pull)
            elif pull is None:
                gaps.append({"reason": problem, "position": f"page {page} item {position}"})
            else:
                gaps.append({**pull, "reason": problem})
        pages = rest.link_pages(response.headers.get("link"))
        if "next" not in pages:
            return pulls, gaps
        if page >= MAX_PAGES:
            raise QualityGateError(
                f"more than {MAX_PAGES * PER_PAGE} open PRs; stopped instead of reading a part."
            )
        page = pages["next"] if pages["next"] > page else page + 1


# The bare repository ----------------------------------------------------------------------


class DiffTooLarge(QualityGateError):
    """One PR's diff is over `MAX_DIFF_BYTES`. That PR is left out; the run goes on."""


def _decode(data, what):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InputError(f"{what} is not UTF-8.") from None


class Repository:
    """One harness-owned bare repository. Counts the git commands it runs."""

    def __init__(self, git_dir, source, token=None, host=None):
        self.git_dir = git_dir
        self.source = source
        self._token = token
        self._host = host  # the GitHub host the user named; the guard allows no other
        self.calls = 0
        self._blobs = {}

    def __repr__(self):
        return f"Repository({str(self.git_dir)!r})"  # the token never appears

    @staticmethod
    def _names(root, owner, name, source):
        key = records.sha256(f"{owner}/{name}\n{source}")[:12]
        git_dir = paths.child(root, "cache", "git", f"{owner}--{name}--{key}.git")
        marker = paths.child(root, "cache", "git", f"{owner}--{name}--{key}.json")
        return git_dir, marker

    @classmethod
    def open(cls, root, owner, name, source, token=None, host=None):
        """The cache entry for (repository, fetch source), created on first use."""
        git_dir, marker = cls._names(root, owner, name, source)
        repository = cls(git_dir, source, token, host)
        if not (git_dir / "HEAD").is_file():
            records.write_json(marker, {"repo": f"{owner}/{name}", "source": source})
            repository.run(["init", "--bare", "--quiet"])
        return repository

    @classmethod
    def existing(cls, root, owner, name, source):
        """The cache entry `spec analyze` filled. Nothing is created or fetched."""
        git_dir, _ = cls._names(root, owner, name, source)
        if git_dir.is_symlink() or not (git_dir / "HEAD").is_file():
            raise InputError(
                "the bare repository `spec analyze` used is gone (cache/git/); run the analysis "
                "again."
            )
        return cls(git_dir, source)

    def run(self, arguments, allowed=(0,), index_file=None):
        """(exit code, stdout). An exit code outside `allowed` stops the stage. `index_file`
        points git at a temporary index instead of the repository's own."""
        self.calls += 1
        code, out = guard.git(
            self.git_dir, arguments, token=self._token, index_file=index_file, host=self._host
        )
        if code not in allowed:
            raise QualityGateError(f"git {arguments[0]} failed with exit code {code}.")
        return code, out

    def fetch(self, refspecs):
        arguments = ["fetch", "--no-tags", "--quiet", "--no-write-fetch-head", self.source]
        code, _ = self.run([*arguments, *refspecs], allowed=range(0, 256))
        return code == 0

    def fetch_open(self, default_branch, numbers):
        """Fetch the default branch and the PR heads. Returns the PR numbers that arrived.

        One fetch carries every ref. If it fails (a PR head that no longer exists makes the
        whole fetch fail), each PR is fetched alone so that only the missing ones are missing.
        """
        main = f"+refs/heads/{default_branch}:{main_ref(default_branch)}"
        heads = {number: f"+refs/pull/{number}/head:{pull_ref(number)}" for number in numbers}
        if self.fetch([main, *heads.values()]):
            return set(numbers)
        if not self.fetch([main]):
            raise QualityGateError(
                f"the default branch {default_branch!r} could not be fetched; nothing was analyzed."
            )
        return {number for number, spec in heads.items() if self.fetch([spec])}

    def rev_parse(self, ref):
        code, out = self.run(["rev-parse", "--verify", "--quiet", ref], allowed=(0, 1))
        sha = out.decode("ascii", "replace").strip()
        return sha if code == 0 and _SHA.fullmatch(sha) else None

    def merge_base(self, first, second):
        code, out = self.run(["merge-base", first, second], allowed=(0, 1))
        sha = out.decode("ascii", "replace").strip()
        return sha if code == 0 and _SHA.fullmatch(sha) else None

    def files(self, sha):
        """Every path of the commit's tree, from a diff against the empty tree."""
        _, out = self.run(
            ["diff", *DIFF_ARGS[:3], "--no-renames", "--name-status", EMPTY_TREE, sha]
        )
        lines = _decode(out, "a path").split("\n")
        return [unquote(line.split("\t", 1)[1]) for line in lines if line]

    def entry(self, sha, path):
        """(mode, blob sha) of `path` in the commit's tree, or None when it is not a blob.

        Read with `git cat-file -p` of the parent tree (`<sha>:<dir>`, or the commit's tree for
        a top-level path), so a symlink (120000) or a submodule (160000) shows as such."""
        directory, _, base = path.rpartition("/")
        if directory:
            code, out = self.run(["cat-file", "-p", f"{sha}:{directory}"], allowed=range(0, 256))
        else:
            code, out = self.run(["cat-file", "-p", sha], allowed=range(0, 256))
            tree = re.match(rb"tree ([0-9a-f]{40})\n", out) if code == 0 else None
            if tree is None:
                return None
            code, out = self.run(["cat-file", "-p", tree.group(1).decode()], allowed=range(0, 256))
        if code != 0:
            return None
        for line in out.decode("utf-8", "replace").splitlines():
            meta, _, name = line.partition("\t")
            fields = meta.split(" ")
            if name == base and len(fields) == 3 and fields[1] == "blob":
                return fields[0], fields[2]
        return None

    def blob(self, sha, path):
        """The file's text at `sha`, or None when it is absent or not UTF-8. Read once."""
        key = (sha, path)
        if key not in self._blobs:
            code, out = self.run(["cat-file", "blob", f"{sha}:{path}"], allowed=range(0, 256))
            if code == 0 and len(out) > MAX_BLOB_BYTES:
                raise QualityGateError(
                    f"{path} at {sha[:12]} is {len(out)} bytes, over the {MAX_BLOB_BYTES}-byte "
                    "cap per file; stopped instead of truncating it."
                )
            text = None
            if code == 0:
                try:
                    text = out.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            self._blobs[key] = text
        return self._blobs[key]


def main_ref(branch):
    return f"refs/sre-harness/heads/{branch}"


def pull_ref(number):
    return f"refs/sre-harness/pull/{number}"


# Diffs ------------------------------------------------------------------------------------


class DiffCache:
    """`git diff <merge base> <head>` once per pair, kept in the run's `diff-cache/`."""

    def __init__(self, repository, run_dir):
        self.repository = repository
        self.run_dir = run_dir
        self.computed = 0
        self.reused = 0
        self.bytes = 0
        self._texts = {}

    def path(self, base, head):
        name = records.sha256(f"{base}..{head}") + ".diff"
        return paths.child(self.run_dir, "diff-cache", name)

    def get(self, base, head):
        key = (base, head)
        if key in self._texts:
            self.reused += 1
            return self._texts[key]
        target = self.path(base, head)
        if target.is_file() and not target.is_symlink():
            data = target.read_bytes()
            self.reused += 1
        else:
            _, data = self.repository.run(["diff", *DIFF_ARGS, base, head])
            self.computed += 1
            if len(data) <= MAX_DIFF_BYTES:
                paths.write_private(target, data)
        if len(data) > MAX_DIFF_BYTES:
            raise DiffTooLarge(
                f"the diff {base[:12]}..{head[:12]} is {len(data)} bytes, over the "
                f"{MAX_DIFF_BYTES}-byte cap; not read instead of truncated."
            )
        self.bytes += len(data)
        if self.bytes > MAX_RUN_DIFF_BYTES:
            raise QualityGateError(
                f"the run's diffs add up to more than {MAX_RUN_DIFF_BYTES} bytes; stopped "
                "instead of dropping some."
            )
        text = data.decode("utf-8", "replace")
        self._texts[key] = text
        return text


def unquote(path):
    """A path as git prints it: C-quoted when it holds unusual bytes."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        raw = codecs.escape_decode(path[1:-1].encode("utf-8"))[0]
        return bytes(raw).decode("utf-8", "replace")
    return path


def _header_path(rest):
    quoted = _QUOTED_PAIR.fullmatch(rest)
    if quoted:
        return unquote(f'"{quoted.group(2)}"')[2:]
    size = (len(rest) - 5) // 2
    if rest[2 + size :] == f" b/{rest[2 : 2 + size]}":
        return rest[2 : 2 + size]
    return rest.split(" b/", 1)[-1]


def parse_diff(text):
    """[{path, status, base, head, hunks}] with the changed line ranges on each side.

    `base` holds (start, count) of every hunk on the merge-base side (count 0 is an insertion
    after `start`); `head` holds inclusive (start, end) ranges of added lines; `hunks` holds
    (base start, base count, head start, head count) per hunk.

    Lines end at "\n" only, as git writes them. After an `@@` header the body is read by its
    counts: its lines are content whatever they contain, never a header or another hunk.
    """
    files, current, old, new = [], None, 0, 0
    for line in text.split("\n"):
        if old > 0 or new > 0:
            tag = line[:1]
            if tag in (" ", "-", "+", "\\"):
                old -= tag in (" ", "-")
                new -= tag in (" ", "+")
                continue
            old = new = 0  # a short body: read the line as a header
        if line.startswith("diff --git "):
            current = {
                "path": _header_path(line[11:]),
                "status": "M",
                "base": [],
                "head": [],
                "hunks": [],
            }
            files.append(current)
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current["status"] = "A"
        elif line.startswith("deleted file mode"):
            current["status"] = "D"
        elif line.startswith("@@"):
            hunk = _HUNK.fullmatch(line)
            if hunk is None:
                continue
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            added_start, added = int(hunk.group(3)), int(hunk.group(4) or 1)
            current["base"].append((start, count))
            current["hunks"].append((start, count, added_start, added))
            if added:
                current["head"].append((added_start, added_start + added - 1))
            old, new = count, added
    return files


def removed_ranges(base):
    return [(start, start + count - 1) for start, count in base if count]


def overlaps(first, second):
    """Whether two hunk lists on the same base touch or overlap, as a three-way merge sees it."""

    def span(start, count):
        return (start, start + count - 1) if count else (start + 0.5, start + 0.5)

    for a in first:
        low_a, high_a = span(*a)
        for b in second:
            low_b, high_b = span(*b)
            if low_b <= high_a + 1 and low_a <= high_b + 1:
                return True
    return False
