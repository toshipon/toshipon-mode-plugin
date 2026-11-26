"""`review collect`: read a period of pull requests and their review history into `raw/`.

Pull requests are listed newest-updated first and the listing stops once it passes `since`, so
the Search API and its 1,000-result cap are never involved. Each page is stored as it arrived
under a name the harness chooses. A page that cannot be read is recorded in `missing`, and the
run ends `partial`.
"""

from datetime import datetime, timezone
from typing import Optional

from .. import paths, records
from ..errors import BoundaryError, QualityGateError
from ..github import graphql
from ..github.rest import ResponseTooLarge, failure_reason, fetch, link_pages

PER_PAGE = 100
# Tried in order when a page is larger than the response cap. Each size divides the one before
# it, so the offset of the first unread item maps to a whole page number at the smaller size.
PAGE_SIZES = (100, 50, 25, 5, 1)
MAX_PAGES = 10000  # per listing; a next link beyond it is treated as unusable
MAX_RUN_BYTES = 256 * 1024 * 1024
PULL_ENDPOINTS = (
    "pull_reviews",
    "pull_review_comments",
    "issue_comments",
    "pull_commits",
    "pull_files",
)
LISTING_QUERY = {"state": "all", "sort": "updated", "direction": "desc"}


class _RunTooLarge(QualityGateError):
    reason = "run_too_large"


def _smaller_page_size(size):
    """The next size in PAGE_SIZES that divides `size`, or None when `size` is already 1."""
    for candidate in PAGE_SIZES:
        if candidate < size and size % candidate == 0:
            return candidate
    return None


class Collector:
    """Holds the run's pages, gaps and rate-limit record while the requests are made."""

    def __init__(self, transport, limiter, run_dir, owner, repo, clock):
        self.transport = transport
        self.limiter = limiter
        self.raw = run_dir / "raw"
        self.owner = owner
        self.repo = repo
        self.clock = clock
        self.pages = []
        self.missing = []
        self.graphql_rates = []
        self.pulls = []
        self.bytes = 0
        self.stopped: Optional[dict] = None

    # The sink interface `github.graphql` writes to.
    def save(self, request, response, body):
        text = records.canonical(
            {
                "schema_version": records.SCHEMA_VERSION,
                "request": request,
                "status": response.status,
                "headers": response.kept_headers(),
                "body": body,
            }
        )
        self.bytes += len(text.encode("utf-8"))
        if self.bytes > MAX_RUN_BYTES:
            raise _RunTooLarge(f"the run's raw pages passed {MAX_RUN_BYTES} bytes; stopped.")
        name = f"{len(self.pages) + 1:04d}.json"
        paths.write_private(paths.child(self.raw, name), text + "\n")
        retrieved = datetime.fromtimestamp(self.clock(), timezone.utc)
        self.pages.append(
            {
                "file": name,
                "request": request,
                "status": response.status,
                "sha256": records.sha256(text + "\n"),
                "retrieved_at": records.iso(retrieved),
            }
        )

    def miss(self, request, response, reason, **extra):
        entry = {"request": request, "status": None if response is None else response.status}
        entry["reason"] = reason
        entry.update(extra)
        self.missing.append(entry)

    def graphql_rate(self, rate):
        entry = {
            key: rate.get(key) for key in ("cost", "remaining") if isinstance(rate.get(key), int)
        }
        if isinstance(rate.get("resetAt"), str):
            entry["reset_at"] = rate["resetAt"][:40]
        self.graphql_rates.append(entry)

    def _values(self, number=None):
        values = {"owner": self.owner, "repo": self.repo}
        if number is not None:
            values["number"] = number
        return values

    def rest_pages(self, endpoint, number=None, base_query=None):
        """Yield each page's items. A failed page is recorded; later pages are still read when
        an earlier `rel="last"` says they exist."""
        page, last, size = 1, None, PER_PAGE
        while True:
            query = {**(base_query or {}), "per_page": size, "page": page}
            request = {"kind": "rest", "endpoint": endpoint, "page": page}
            if size != PER_PAGE:
                request["per_page"] = size
            if number is not None:
                request["number"] = number
            values = self._values(number)
            try:
                response = fetch(
                    self.limiter, request, lambda: self.transport.rest(endpoint, values, query)
                )
            except ResponseTooLarge:
                smaller = _smaller_page_size(size)
                if smaller is None:
                    raise
                page = (page - 1) * size // smaller + 1
                last = None if last is None else last * size // smaller
                size = smaller
                continue
            links = link_pages(response.headers.get("link"))
            items = self._items(request, response)
            if items is None:
                if last is not None and page < last:
                    page += 1
                    continue
                return
            yield items
            last = max(last or 0, links.get("last", 0)) or None
            following = links.get("next")
            if following is None:
                return
            if following <= page or following > MAX_PAGES:
                self.miss(request, None, "unusable_next_link")
                return
            page = following

    def _items(self, request, response):
        reason = failure_reason(response)
        if reason is not None:
            self.miss(request, response, reason)
            return None
        try:
            items = response.data()
        except ValueError:
            self.miss(request, response, "body_not_json")
            return None
        if not isinstance(items, list):
            self.miss(request, response, "unexpected_shape")
            return None
        self.save(request, response, items)
        return items

    def select_pulls(self, since, until):
        """Newest-updated first; stop at the first page that reaches back past `since`."""
        chosen = []
        for items in self.rest_pages("pulls", base_query=LISTING_QUERY):
            older = False
            for item in items:
                updated = item.get("updated_at") if isinstance(item, dict) else None
                number = item.get("number") if isinstance(item, dict) else None
                if not isinstance(updated, str) or not isinstance(number, int):
                    continue
                if updated < since:
                    older = True
                elif updated <= until and number not in chosen:
                    chosen.append(number)
            if older:
                break
        return chosen

    def run(self, since, until):
        self.pulls = self.select_pulls(since, until)
        for number in self.pulls:
            for endpoint in PULL_ENDPOINTS:
                for _ in self.rest_pages(endpoint, number):
                    pass
            graphql.walk_threads(self.transport, self.limiter, self, self.owner, self.repo, number)


def collect(transport, limiter, run_dir, run_id, repo, since, until, source, clock):
    """Run the requests and write `collect.json`. Returns the collect record."""
    owner, name = repo.split("/", 1)
    collector = Collector(transport, limiter, run_dir, owner, name, clock)
    refused = None
    try:
        collector.run(since, until)
    except QualityGateError as error:
        collector.stopped = {"reason": getattr(error, "reason", "stopped"), "message": str(error)}
    except BoundaryError as error:
        collector.stopped = {"reason": "boundary", "message": str(error)}
        refused = error
    partial = bool(collector.missing) or collector.stopped is not None
    record = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": records.iso(),
        "status": "partial" if partial else "complete",
        "repo": repo,
        "source": source,
        "since": since,
        "until": until,
        "pulls": collector.pulls,
        "page_count": len(collector.pages),
        "pages": collector.pages,
        "missing": collector.missing,
        "rate_limit": {**limiter.record(), "graphql": collector.graphql_rates},
    }
    if collector.stopped is not None:
        record["stopped"] = collector.stopped
    records.validate("collect", record)
    records.write_json(paths.child(run_dir, "collect.json"), record)
    if refused is not None:
        raise refused
    return record
