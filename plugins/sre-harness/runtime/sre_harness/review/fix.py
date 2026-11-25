"""Link each inline review thread to the commit that addressed it, from collected pages only.

Nothing here talks to GitHub: the PR's commit list (`pull_commits`) and changed files
(`pull_files`) were stored by collect. Three signals are tried in order, and the first one that
names any commit decides the method:

1. `suggestion_applied`: a comment carries a suggestion, a later commit has GitHub's
   "Apply suggestion(s) ..." message, and the PR's patch for the path adds the suggested lines.
2. `reply_link`: a reply names a commit of the PR by SHA (7 to 40 hex digits).
3. `same_path_later_commit`: the PR changed the thread's path, and commits came after the
   comment. The commit list has no per-commit files, so this is the weakest signal.

One candidate is `found`; several are `ambiguous` (kept, never guessed); none is `not_found`.
When the commit the thread was written on is not among the PR's commits (a force push rewrote
it), the thread is `not_found` and `fix.before` goes into `missing`.
"""

import re

from .. import paths, records
from .raw import RawPages

APPLY_SUGGESTION = re.compile(r"Apply suggestions? (?:from|to)\b", re.IGNORECASE)
SHA_MENTION = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{7,40}(?![0-9a-zA-Z])")
HUNK = re.compile(r"@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SHA = re.compile(r"[0-9a-f]{40}")
NO_PULL = {"commits": [], "files": {}, "body": None}


def _commit(item):
    sha = item.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return None
    detail = item.get("commit")
    detail = detail if isinstance(detail, dict) else {}
    message = detail.get("message") if isinstance(detail.get("message"), str) else ""
    date = None
    for role in ("committer", "author"):
        person = detail.get(role)
        value = person.get("date") if isinstance(person, dict) else None
        if isinstance(value, str) and _TIME.fullmatch(value):
            date = value
            break
    return {"sha": sha, "message": message, "date": date}


def pull_contexts(run_dir):
    """{PR number: {commits, files (path -> patch or None), body}} from the run's raw pages."""
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    raw = RawPages(run_dir, collect)
    bodies = {}
    for _, item in raw.items("pulls"):
        number = item.get("number")
        if isinstance(number, int) and number not in bodies:
            bodies[number] = item.get("body") if isinstance(item.get("body"), str) else None
    contexts = {}
    for number in collect["pulls"]:
        commits, seen = [], set()
        for _, item in raw.items("pull_commits", number):
            commit = _commit(item)
            if commit is not None and commit["sha"] not in seen:
                seen.add(commit["sha"])
                commits.append(commit)
        files = {}
        for _, item in raw.items("pull_files", number):
            name = item.get("filename")
            if isinstance(name, str) and name not in files:
                files[name] = item.get("patch") if isinstance(item.get("patch"), str) else None
        contexts[number] = {"commits": commits, "files": files, "body": bodies.get(number)}
    return contexts


def added_span(patch, lines):
    """[first, last] new-file line numbers where `lines` were added as one run, or None."""
    if not patch or not lines:
        return None
    wanted = [line.rstrip() for line in lines]
    added, number = [], None
    for text in patch.split("\n"):
        header = HUNK.match(text)
        if header:
            number = int(header.group(1))
            continue
        if number is None or text.startswith("\\"):
            continue
        if text.startswith("+"):
            added.append((number, text[1:].rstrip()))
            number += 1
        elif not text.startswith("-"):
            number += 1
    for start in range(len(added) - len(wanted) + 1):
        window = added[start : start + len(wanted)]
        numbers = [line for line, _ in window]
        if [text for _, text in window] == wanted and numbers == list(
            range(numbers[0], numbers[0] + len(wanted))
        ):
            return [numbers[0], numbers[-1]]
    return None


def _decide(method, candidates, before, path, span=None):
    shas = sorted(set(candidates))
    if not shas:
        return None
    if len(shas) > 1:
        return {"status": "ambiguous", "method": method, "commits": shas, "before": before}
    after = {"sha": shas[0], "path": path}
    if span:
        after["range"] = span
    return {"status": "found", "method": method, "commits": shas, "before": before, "after": after}


def link(thread, pull):
    """(fix, names to add to `missing`) for one inline thread."""
    detail = thread["thread"]
    path, original = detail.get("path"), detail.get("original_commit_id")
    shas = [commit["sha"] for commit in pull["commits"]]
    if not path or original not in shas:
        return {"status": "not_found", "commits": []}, ["fix.before"]
    before = {"sha": original, "path": path}
    if detail.get("original_line"):
        before["range"] = [detail["original_line"], detail["original_line"]]
    comments = thread["comments"]
    asked = comments[0].get("created_at")
    later = [c for c in pull["commits"] if asked and c["date"] and c["date"] > asked]
    patch = pull["files"].get(path)

    suggestions = [
        comment["suggestion"]["lines"] for comment in comments if "suggestion" in comment
    ]
    applied = [c["sha"] for c in later if APPLY_SUGGESTION.match(c["message"])]
    for lines in suggestions:
        span = added_span(patch, lines)
        if applied and span:
            return _decide("suggestion_applied", applied, before, path, span), []

    named = set()
    for reply in comments[1:]:
        for mention in SHA_MENTION.findall(reply.get("body") or ""):
            named.update(sha for sha in shas if sha.startswith(mention) and sha != original)
    if named:
        return _decide("reply_link", named, before, path), []

    if path in pull["files"]:
        decided = _decide("same_path_later_commit", [c["sha"] for c in later], before, path)
        if decided:
            return decided, []
    return {"status": "not_found", "commits": [], "before": before}, []


def attach(run_dir, threads, contexts=None):
    """Threads with `fix` filled for every inline thread. Other kinds are returned unchanged."""
    contexts = pull_contexts(run_dir) if contexts is None else contexts
    linked = []
    for thread in threads:
        if thread["kind"] != "inline_thread":
            linked.append(thread)
            continue
        fix, missing = link(thread, contexts.get(thread["pr"]["number"], NO_PULL))
        gaps = list(dict.fromkeys(thread["missing"] + missing))
        linked.append({**thread, "fix": fix, "missing": gaps})
    return linked
