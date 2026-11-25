"""`review normalize`: raw pages to review-thread records.

Three kinds come out: `inline_thread` (a review thread on a diff line), `review_summary` (the
body and verdict of a review) and `issue_comment` (a comment on the PR conversation). Comments
are keyed by `node_id`, so the same comment read over REST and GraphQL, or twice across a page
boundary, becomes one. A field the source left out is never filled with a default; its name
goes into `missing`. A record that breaks the schema goes to `rejected.jsonl` with reasons.

Comment bodies are external input. They are copied as data and never interpreted.
"""

import re

from .. import paths, records
from ..errors import InputError
from ..github import graphql
from .raw import RawPages

SUGGESTION = re.compile(r"```suggestion[^\n]*\n(.*?)```", re.DOTALL)
ASSOCIATION = {
    "OWNER": "owner",
    "MEMBER": "member",
    "COLLABORATOR": "collaborator",
    "CONTRIBUTOR": "contributor",
    "FIRST_TIME_CONTRIBUTOR": "first_time_contributor",
    "FIRST_TIMER": "first_time_contributor",
    "MANNEQUIN": "mannequin",
    "NONE": "none",
}
REVIEW_STATE = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}
KIND_ORDER = {"review_summary": 0, "inline_thread": 1, "issue_comment": 2}
_ABSENT = object()


def _get(value, key):
    return value.get(key, _ABSENT) if isinstance(value, dict) else _ABSENT


def _put(target, key, value, missing, label):
    """Copy a source field. Absent stays absent and is listed; null is kept when it is a value."""
    if value is _ABSENT:
        missing.append(label)
    else:
        target[key] = value


def _author(user, login_key, type_key, bot_value):
    """(fields, missing). A null author is GitHub's deleted user, shown as `ghost`."""
    if user is _ABSENT:
        return {}, ["author"]
    if user is None or (isinstance(user, dict) and user.get(login_key) == "ghost"):
        return {"author_type": "ghost"}, []
    if not isinstance(user, dict):
        return {}, ["author"]
    fields = {"author_type": "bot" if user.get(type_key) == bot_value else "user"}
    if isinstance(user.get(login_key), str):
        fields["author_login"] = user[login_key]
    return fields, ([] if "author_login" in fields else ["author_login"])


def _association(value):
    if value is _ABSENT or value is None:
        return _ABSENT
    return ASSOCIATION.get(str(value), str(value).lower())


def _suggestion(body):
    if not isinstance(body, str):
        return None
    match = SUGGESTION.search(body)
    return None if match is None else {"lines": match.group(1).rstrip("\n").split("\n")}


def _oid(value):
    if value is _ABSENT:
        return _ABSENT
    return value.get("oid") if isinstance(value, dict) else None


def rest_comment(item):
    """A REST review comment, issue comment or review body in the common comment shape."""
    comment, missing = {}, []
    fields, gaps = _author(_get(item, "user"), "login", "type", "Bot")
    comment.update(fields)
    missing += gaps
    _put(comment, "id", _get(item, "node_id"), missing, "id")
    created = _get(item, "created_at")
    if created is _ABSENT:
        created = _get(item, "submitted_at")
    _put(comment, "created_at", created, missing, "created_at")
    _put(comment, "body", _get(item, "body"), missing, "body")
    _put(
        comment,
        "author_association",
        _association(_get(item, "author_association")),
        missing,
        "author_association",
    )
    for key in ("commit_id", "original_commit_id"):
        value = _get(item, key)
        if value is not _ABSENT:
            comment[key] = value
    suggestion = _suggestion(comment.get("body"))
    if suggestion:
        comment["suggestion"] = suggestion
    return comment, missing


def graphql_comment(node):
    comment, missing = {}, []
    fields, gaps = _author(_get(node, "author"), "login", "__typename", "Bot")
    comment.update(fields)
    missing += gaps
    _put(comment, "id", _get(node, "id"), missing, "id")
    _put(comment, "created_at", _get(node, "createdAt"), missing, "created_at")
    _put(comment, "body", _get(node, "body"), missing, "body")
    _put(
        comment,
        "author_association",
        _association(_get(node, "authorAssociation")),
        missing,
        "author_association",
    )
    reply = _get(node, "replyTo")
    if isinstance(reply, dict) and isinstance(reply.get("id"), str):
        comment["in_reply_to"] = reply["id"]
    for key, source in (("original_commit_id", "originalCommit"), ("commit_id", "commit")):
        value = _oid(_get(node, source))
        if value is not _ABSENT:
            comment[key] = value
    suggestion = _suggestion(comment.get("body"))
    if suggestion:
        comment["suggestion"] = suggestion
    return comment, missing


def _merge(primary, secondary):
    """GraphQL first; REST fills what GraphQL left out. A gap REST filled is no longer a gap."""
    comment = dict(secondary[0])
    for key, value in primary[0].items():
        if value is not None or key not in comment:
            comment[key] = value
    keys = {"author": "author_type"}
    gaps = [
        label
        for label in dict.fromkeys(primary[1] + secondary[1])
        if keys.get(label, label) not in comment
    ]
    return comment, gaps


class Normalizer:
    def __init__(self, run_dir, collect, now):
        self.collect = collect
        self.raw = RawPages(run_dir, collect)
        self.now = now
        self.repo = collect["repo"]
        host = collect["source"].get("host") or "github.com"
        self.source_id = f"github:{host}/{self.repo}"
        self.retrieved = {page["file"]: page["retrieved_at"] for page in collect["pages"]}
        self.gaps = {}
        for entry in collect["missing"]:
            request = entry["request"]
            label = request.get("endpoint") or request.get("query") or "request"
            self.gaps.setdefault(request.get("number"), set()).add(f"source.{label}")

    def pulls(self):
        found = {}
        for name, item in self.raw.items("pulls"):
            number = item.get("number")
            if number in self.collect["pulls"] and number not in found:
                found[number] = (name, item)
        return found

    def pr(self, item):
        pr, missing = {"number": item["number"]}, []
        for key in ("state", "created_at", "merged_at"):
            _put(pr, key, _get(item, key), missing, f"pr.{key}")
        fields, gaps = _author(_get(item, "user"), "login", "type", "Bot")
        pr.update(fields)
        missing += [f"pr.{gap}" for gap in gaps]
        for key, side in (("base_sha", "base"), ("head_sha", "head")):
            _put(pr, key, _get(_get(item, side), "sha"), missing, f"pr.{key}")
        head_repo = _get(_get(item, "head"), "repo")
        if isinstance(head_repo, dict) and isinstance(head_repo.get("full_name"), str):
            pr["head_repo"] = head_repo["full_name"]
        else:
            missing.append("pr.head_repo")  # a deleted fork leaves head.repo null
        return pr, missing

    def provenance(self, name):
        return {
            "source_id": self.source_id,
            "locator": f"raw/{name}",
            "content_sha256": self.raw.sha[name],
            "retrieved_at": self.retrieved[name],
        }

    def inline_threads(self, number):
        """GraphQL threads joined with REST review comments by node_id."""
        rest = {}
        rest_file = {}
        by_database_id = {}
        for name, item in self.raw.items("pull_review_comments", number):
            node = item.get("node_id")
            if not isinstance(node, str) or node in rest:
                continue
            rest[node] = item
            rest_file[node] = name
            if isinstance(item.get("id"), int):
                by_database_id[item["id"]] = node
        threads = {}
        owner = {}
        for name, request, body in self.raw.graphql:
            if request.get("number") != number:
                continue
            if request["query"] == graphql.THREADS:
                connection = graphql.dig(body, "data", "repository", "pullRequest", "reviewThreads")
                for node in (connection or {}).get("nodes") or []:
                    thread_id = node.get("id") if isinstance(node, dict) else None
                    if not isinstance(thread_id, str) or thread_id in threads:
                        continue
                    threads[thread_id] = {"node": node, "file": name, "comments": {}}
                    for comment in graphql.dig(node, "comments", "nodes") or []:
                        self._gql_into(threads[thread_id], owner, thread_id, comment)
            else:
                thread_id = request.get("thread")
                if thread_id in threads:
                    for comment in graphql.dig(body, "data", "node", "comments", "nodes") or []:
                        self._gql_into(threads[thread_id], owner, thread_id, comment)
        orphans = {}
        for node, item in rest.items():
            root = self._root(item, rest, by_database_id)
            thread_id = owner.get(node) or owner.get(root)
            if thread_id is not None:
                threads[thread_id]["comments"].setdefault(node, [None])
                threads[thread_id]["comments"][node].append(item)
                owner[node] = thread_id
            else:
                orphans.setdefault(root, []).append(node)
        for thread_id, thread in threads.items():
            yield self._thread_record(thread_id, thread, rest, by_database_id)
        for root, nodes in orphans.items():
            thread = {
                "node": _ABSENT,
                "file": rest_file[root],
                "comments": {node: [None, rest[node]] for node in nodes},
            }
            yield self._thread_record(root, thread, rest, by_database_id)

    @staticmethod
    def _gql_into(thread, owner, thread_id, comment):
        node = comment.get("id") if isinstance(comment, dict) else None
        if isinstance(node, str) and node not in thread["comments"]:
            thread["comments"][node] = [comment]
            owner[node] = thread_id

    @staticmethod
    def _root(item, rest, by_database_id):
        node, seen = item["node_id"], set()
        while node not in seen:
            seen.add(node)
            parent = by_database_id.get(rest[node].get("in_reply_to_id")) if node in rest else None
            if parent is None:
                return node
            node = parent
        return node

    def _thread_record(self, thread_id, thread, rest, by_database_id):
        missing = []
        source = thread["node"]
        comments = []
        for node, (gql, *rest_items) in thread["comments"].items():
            parts = []
            if gql is not None:
                parts.append(graphql_comment(gql))
            if rest_items:
                comment, gaps = rest_comment(rest_items[0])
                parent = by_database_id.get(rest_items[0].get("in_reply_to_id"))
                if parent is not None:
                    comment["in_reply_to"] = parent
                elif rest_items[0].get("in_reply_to_id") is not None:
                    gaps.append("in_reply_to")
                parts.append((comment, gaps))
            comment, gaps = parts[0] if len(parts) == 1 else _merge(parts[0], parts[1])
            comments.append((comment, gaps))
        comments.sort(key=lambda pair: (pair[0].get("created_at") or "", pair[0].get("id") or ""))
        for index, (_, gaps) in enumerate(comments):
            missing += [f"comments[{index}].{gap}" for gap in gaps]
        root = rest.get(comments[0][0].get("id")) if comments else None
        detail = {}
        if source is _ABSENT:
            missing += ["thread.is_resolved", "thread.is_outdated", "thread.resolved_by"]
        else:
            for key, field in (("is_resolved", "isResolved"), ("is_outdated", "isOutdated")):
                value = _get(source, field)
                _put(detail, key, _ABSENT if value is None else value, missing, f"thread.{key}")
            resolver = _get(source, "resolvedBy")
            if resolver is _ABSENT:
                missing.append("thread.resolved_by")
            else:
                detail["resolved_by"] = (
                    resolver.get("login") if isinstance(resolver, dict) else None
                )
        pairs = (
            ("path", "path", "path"),
            ("line", "line", "line"),
            ("original_line", "originalLine", "original_line"),
        )
        for key, gql_key, rest_key in pairs:
            value = _get(source, gql_key)
            if value is _ABSENT:
                value = _get(root, rest_key)
            _put(detail, key, value, missing, f"thread.{key}")
        for key in ("diff_hunk", "original_commit_id", "commit_id"):
            value = _get(root, key)
            if value is _ABSENT and key != "diff_hunk" and comments:
                value = comments[0][0].get(key, _ABSENT)
            _put(detail, key, value, missing, f"thread.{key}")
        return {
            "kind": "inline_thread",
            "node_id": thread_id,
            "thread": detail,
            "comments": [comment for comment, _ in comments],
            "missing": missing,
            "file": thread["file"],
        }

    def reviews(self, number):
        seen = set()
        for name, item in self.raw.items("pull_reviews", number):
            node = item.get("node_id")
            if node in seen:
                continue
            seen.add(node)
            state = item.get("state")
            body = item.get("body")
            if state == "PENDING" or (state == "COMMENTED" and not body):
                continue  # an empty COMMENTED review only carries inline comments
            comment, gaps = rest_comment(item)
            review = {"state": REVIEW_STATE.get(str(state), str(state).lower())}
            yield {
                "kind": "review_summary",
                "node_id": node,
                "review": review,
                "comments": [comment],
                "missing": [f"comments[0].{gap}" for gap in gaps],
                "file": name,
            }

    def issue_comments(self, number):
        seen = set()
        for name, item in self.raw.items("issue_comments", number):
            node = item.get("node_id")
            if node in seen:
                continue
            seen.add(node)
            comment, gaps = rest_comment(item)
            yield {
                "kind": "issue_comment",
                "node_id": node,
                "comments": [comment],
                "missing": [f"comments[0].{gap}" for gap in gaps],
                "file": name,
            }

    def run(self):
        drafts = []
        for number, (_, item) in sorted(self.pulls().items()):
            pr, pr_missing = self.pr(item)
            extra = sorted(self.gaps.get(number, set()))
            for draft in (
                *self.reviews(number),
                *self.inline_threads(number),
                *self.issue_comments(number),
            ):
                draft["pr"] = pr
                draft["missing"] = list(dict.fromkeys(pr_missing + draft["missing"] + extra))
                drafts.append(draft)
        drafts.sort(key=_order)
        accepted, rejected, seen = [], [], set()
        for draft in drafts:
            if draft["node_id"] in seen:
                continue
            seen.add(draft["node_id"])
            name = draft.pop("file")
            record = {
                "schema_version": records.SCHEMA_VERSION,
                "id": records.record_id("RT", len(accepted) + 1),
                "run_id": self.collect["run_id"],
                "created_at": self.now,
                "provenance": self.provenance(name),
                "repo": self.repo,
                **draft,
            }
            problems = records.violations("review-thread", record)
            if problems:
                rejected.append(
                    {
                        "schema_version": records.SCHEMA_VERSION,
                        "run_id": self.collect["run_id"],
                        "kind": draft["kind"],
                        "node_id": draft["node_id"],
                        "pr": draft["pr"]["number"],
                        "locator": f"raw/{name}",
                        "reasons": problems,
                    }
                )
            else:
                accepted.append(record)
        return accepted, rejected


def _order(draft):
    first = draft["comments"][0] if draft["comments"] else {}
    created = first.get("created_at") if isinstance(first.get("created_at"), str) else ""
    return (draft["pr"]["number"], KIND_ORDER[draft["kind"]], created, str(draft["node_id"]))


def normalize(run_dir):
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    problems = records.violations("collect", collect)
    if problems:
        raise InputError("collect.json is invalid: " + "; ".join(problems[:5]))
    accepted, rejected = Normalizer(run_dir, collect, records.iso()).run()
    records.write_jsonl(paths.child(run_dir, "threads.jsonl"), accepted)
    records.write_jsonl(paths.child(run_dir, "rejected.jsonl"), rejected)
    return {
        "run_id": collect["run_id"],
        "collect_status": collect["status"],
        "threads": len(accepted),
        "rejected": len(rejected),
    }
