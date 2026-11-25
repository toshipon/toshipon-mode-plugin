"""`review split`: fix the time split between generation and evaluation (`split.json`).

The split is made by the time of each event, not by merge dates:

- Generation side: PRs created before `t1`. Their comments and commits at or after `t1`, or
  without a time, are not used by `analyze --split` or `generate --split`; their numbers are
  recorded. A PR with a commit at or after `t1` also loses its patches, because a patch is the
  PR's final state and may contain that later commit.
- Evaluation side: PRs created at or after `t1 + gap_days`.
- PRs in between (the gap) and PRs without a creation time belong to neither side.

The file holds the period, the PR numbers and `sha256`, the hash of every field but `created_at`
and `sha256` itself. Later stages refuse a split whose hash does not match its content.
"""

from datetime import datetime, timedelta, timezone

from .. import paths, records
from ..errors import InputError
from . import fix
from .raw import RawPages

FILE = "split.json"
MAX_SPLIT_BYTES = 4 * 1024 * 1024
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _shift(moment, days):
    parsed = datetime.strptime(moment, _FORMAT).replace(tzinfo=timezone.utc)
    return (parsed + timedelta(days=days)).strftime(_FORMAT)


def _before(value, t1):
    return isinstance(value, str) and value < t1


def created_times(run_dir, collect):
    """{PR number: created_at or None} for the PRs collect selected."""
    found = {}
    for _, item in RawPages(run_dir, collect).items("pulls"):
        number = item.get("number")
        if number in collect["pulls"] and number not in found:
            created = item.get("created_at")
            found[number] = created if isinstance(created, str) else None
    return {number: found.get(number) for number in collect["pulls"]}


def seal(split):
    body = {key: value for key, value in split.items() if key not in {"created_at", "sha256"}}
    return {**split, "sha256": records.fingerprint(body)}


def make(run_dir, t1, gap_days=0):
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    threads = records.read_jsonl(paths.child(run_dir, "threads.jsonl"))
    contexts = fix.pull_contexts(run_dir)
    created = created_times(run_dir, collect)
    evaluation_from = _shift(t1, gap_days)
    generation, evaluation, gap, undated = [], [], [], []
    for number in sorted(created):
        moment = created[number]
        if moment is None:
            undated.append(number)
        elif moment < t1:
            generation.append(number)
        elif moment >= evaluation_from:
            evaluation.append(number)
        else:
            gap.append(number)
    dropped = {"comments": 0, "commits": 0, "undated_comments": 0, "undated_commits": 0}
    chosen = set(generation)
    for thread in threads:
        if thread["pr"]["number"] in chosen:
            for comment in thread["comments"]:
                _count(dropped, "comments", comment.get("created_at"), t1)
    for number in generation:
        for commit in contexts.get(number, fix.NO_PULL)["commits"]:
            _count(dropped, "commits", commit["date"], t1)
    split = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": run_dir.name,
        "created_at": records.iso(),
        "repo": collect["repo"],
        "period": {"since": collect["since"], "until": collect["until"]},
        "t1": t1,
        "gap_days": gap_days,
        "evaluation_from": evaluation_from,
        "generation": {"prs": generation, "dropped": dropped},
        "evaluation": {"prs": evaluation},
        "excluded": {"gap_prs": gap, "undated_prs": undated},
    }
    split = records.validate("review-split", seal(split))
    records.write_json(paths.child(run_dir, FILE), split)
    return split


def _count(dropped, kind, moment, t1):
    if not isinstance(moment, str):
        dropped[f"undated_{kind}"] += 1
    elif moment >= t1:
        dropped[kind] += 1


def load(path, repo=None):
    """A split file given on the command line: schema, hash, and the run's repository."""
    try:
        size = path.stat().st_size
    except OSError:
        raise InputError(f"cannot read the split file: {path}") from None
    if size > MAX_SPLIT_BYTES:
        raise InputError(f"the split file is over {MAX_SPLIT_BYTES} bytes.")
    split = records.validate("review-split", records.read_json(path))
    if seal(split)["sha256"] != split["sha256"]:
        raise InputError("the split file changed after `review split` wrote it (hash mismatch).")
    if repo is not None and split["repo"] != repo:
        raise InputError(f"the split is for {split['repo']}, not for this run's {repo}.")
    return split


def restrict(threads, contexts, split):
    """What the generation side may see: (threads, contexts, counts of what was left out)."""
    t1, chosen = split["t1"], set(split["generation"]["prs"])
    left_out = {"threads": 0, "comments": 0, "commits": 0, "patches": 0}
    kept = []
    for thread in threads:
        if thread["pr"]["number"] not in chosen:
            left_out["threads"] += 1
            continue
        comments = [c for c in thread["comments"] if _before(c.get("created_at"), t1)]
        left_out["comments"] += len(thread["comments"]) - len(comments)
        # A thread whose first comment is late was raised after t1: none of it is used.
        if not comments or comments[0] is not thread["comments"][0]:
            left_out["threads"] += 1
            left_out["comments"] += len(comments)
            continue
        kept.append({**thread, "comments": comments})
    narrowed = {}
    for number, pull in contexts.items():
        if number not in chosen:
            continue
        commits = [c for c in pull["commits"] if _before(c["date"], t1)]
        left_out["commits"] += len(pull["commits"]) - len(commits)
        files = pull["files"]
        if len(commits) < len(pull["commits"]):
            left_out["patches"] += len(files)
            files = {}
        narrowed[number] = {**pull, "commits": commits, "files": files}
    return kept, narrowed, left_out
