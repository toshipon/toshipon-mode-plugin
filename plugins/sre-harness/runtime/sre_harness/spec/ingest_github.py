"""GitHub to documents: a PR's body (`github_pr`) and its conversation comments
(`github_issue_comment`), read through the same transports as `review collect`.

Only the `pull` and `issue_comments` GET templates in `guard.REST_ENDPOINTS` are used. A page
that cannot be read is recorded as `input_missing` with the HTTP reason; nothing is filled in.
"""

import re

from .. import records
from ..github.rest import failure_reason, fetch, link_pages
from . import ingest_file

PER_PAGE = 100
MAX_PAGES = 100
_SHA = re.compile(r"[0-9a-f]{40}")


class _Missing(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _get(transport, limiter, endpoint, values, query, request):
    response = fetch(limiter, request, lambda: transport.rest(endpoint, values, query))
    reason = failure_reason(response)
    if reason is not None:
        raise _Missing(reason)
    try:
        return response, response.data()
    except ValueError:
        raise _Missing("body_not_json") from None


def _pr_document(source_id, repo, number, response, data, now):
    missing, sections = [], []
    title = data.get("title")
    if not isinstance(title, str):
        missing.append("title")
        title = ""
    body = data.get("body")
    if isinstance(body, str) and body.strip():
        sections = ingest_file.markdown_sections(body)
    else:
        missing.append("body")
    provenance = {
        "source_id": source_id,
        "locator": f"repos/{repo}/pulls/{number}",
        "content_sha256": records.sha256(response.body),
        "retrieved_at": now,
    }
    head = data["head"].get("sha") if isinstance(data.get("head"), dict) else None
    if isinstance(head, str) and _SHA.fullmatch(head):
        provenance["sha"] = head
    else:
        missing.append("head.sha")
    return {
        "kind": "github_pr",
        "title": title[:300],
        "sections": ingest_file.numbered(sections),
        "provenance": provenance,
        "missing": missing,
    }


def _comment_document(source_id, repo, number, items, now):
    sections, missing = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            missing.append(f"comments[{index}]")
            continue
        user = item.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        login = login if isinstance(login, str) else "ghost"
        body = item.get("body")
        if not isinstance(body, str):
            missing.append(f"comments[{index}].body")
            body = ""
        sections.append(
            {"heading": login[:100], "text": body, "locator": f"issue_comment {item['id']}"}
        )
    return {
        "kind": "github_issue_comment",
        "title": f"{repo}#{number} conversation",
        "sections": ingest_file.numbered(sections),
        "provenance": {
            "source_id": source_id,
            "locator": f"repos/{repo}/issues/{number}/comments",
            "content_sha256": records.fingerprint(items),
            "retrieved_at": now,
        },
        "missing": missing,
    }


def read_pull(transport, limiter, entry, now):
    """(documents, input_missing) for one `{repo, pull}` manifest entry."""
    repo, number = entry["repo"], entry["pull"]
    owner, name = repo.split("/", 1)
    values = {"owner": owner, "repo": name, "number": number}
    source_id = f"github:{repo}#{number}"
    request = {"kind": "rest", "endpoint": "pull", "number": number}
    try:
        response, data = _get(transport, limiter, "pull", values, {}, request)
    except _Missing as gap:
        locator = f"repos/{repo}/pulls/{number}"
        return [], [{"source_id": source_id, "locator": locator, "reason": gap.reason}]
    if not isinstance(data, dict):
        locator = f"repos/{repo}/pulls/{number}"
        return [], [{"source_id": source_id, "locator": locator, "reason": "unexpected_shape"}]
    documents = [_pr_document(source_id, repo, number, response, data, now)]
    items, gaps, page = [], [], 1
    while page <= MAX_PAGES:
        query = {"per_page": PER_PAGE, "page": page}
        request = {"kind": "rest", "endpoint": "issue_comments", "number": number, "page": page}
        locator = f"repos/{repo}/issues/{number}/comments?page={page}"
        try:
            response, data = _get(transport, limiter, "issue_comments", values, query, request)
        except _Missing as gap:
            gaps.append({"source_id": source_id, "locator": locator, "reason": gap.reason})
            break
        if not isinstance(data, list):
            gaps.append({"source_id": source_id, "locator": locator, "reason": "unexpected_shape"})
            break
        items += data
        following = link_pages(response.headers.get("link")).get("next")
        if following is None or following <= page:
            break
        page = following
    documents.append(_comment_document(source_id, repo, number, items, now))
    return documents, gaps
