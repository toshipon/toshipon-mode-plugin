"""Review threads over GraphQL, walked with two cursors: threads per PR, comments per thread.

Only the bundled queries in `queries/` are sent (`guard.graphql_query` checks that each is a
`query` operation). Cursors the server returns travel back as variables, which the guard
checks against its own pattern.
"""

from datetime import datetime

from .rest import failure_reason, fetch

THREADS = "review_threads"
COMMENTS = "thread_comments"
MAX_CURSOR_PAGES = 1000


def dig(value, *keys):
    """`value[k1][k2]...`, or None as soon as a level is not an object."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _epoch(text):
    try:
        return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except (TypeError, ValueError):
        return None


def _error_types(body):
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return sorted(
        {str(error.get("type", "UNKNOWN"))[:40] for error in errors if isinstance(error, dict)}
    )


def _rate_limited_error(limiter):
    """GraphQL reports a primary limit as 200 with an error of type RATE_LIMITED."""

    def limited(response):
        if not response.ok:
            return False
        try:
            body = response.data()
        except ValueError:
            return False
        if "RATE_LIMITED" not in _error_types(body):
            return False
        reset = response.headers.get("x-ratelimit-reset", "")
        epoch = int(reset) if reset.isdigit() else int(limiter.clock()) + 60
        limiter.hold_until(epoch, "graphql")
        return True

    return limited


def query(transport, limiter, sink, name, variables, request):
    """One bundled query. Saves the page and returns `data`, or records why it is missing."""
    response = fetch(
        limiter, request, lambda: transport.graphql(name, variables), _rate_limited_error(limiter)
    )
    reason = failure_reason(response)
    if reason is not None:
        sink.miss(request, response, reason)
        return None
    try:
        body = response.data()
    except ValueError:
        sink.miss(request, response, "body_not_json")
        return None
    if not isinstance(body, dict):
        sink.miss(request, response, "unexpected_shape")
        return None
    rate = dig(body, "data", "rateLimit")
    if isinstance(rate, dict):
        sink.graphql_rate(rate)
        reset = _epoch(rate.get("resetAt"))
        if rate.get("remaining") == 0 and reset is not None:
            limiter.hold_until(reset, "graphql")
    types = _error_types(body)
    if types:
        sink.miss(request, response, "graphql_errors", types=types)
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    sink.save(request, response, body)
    return data


def _next_cursor(info, seen):
    if not isinstance(info, dict) or info.get("hasNextPage") is not True:
        return None, False
    cursor = info.get("endCursor")
    if not isinstance(cursor, str) or not cursor or cursor in seen or len(seen) >= MAX_CURSOR_PAGES:
        return None, True
    seen.add(cursor)
    return cursor, False


def walk_comments(transport, limiter, sink, thread_id, cursor, number):
    seen = {cursor}
    while cursor is not None:
        request = {
            "kind": "graphql",
            "query": COMMENTS,
            "number": number,
            "thread": thread_id,
            "cursor": cursor,
        }
        data = query(
            transport, limiter, sink, COMMENTS, {"thread": thread_id, "cursor": cursor}, request
        )
        comments = dig(data, "node", "comments")
        if not isinstance(comments, dict):
            if data is not None:
                sink.miss(request, None, "no_comments")
            return
        cursor, broken = _next_cursor(comments.get("pageInfo"), seen)
        if broken:
            sink.miss(request, None, "unusable_cursor")


def walk_threads(transport, limiter, sink, owner, name, number):
    """Every review thread of one PR and every comment in each thread."""
    cursor = None
    seen = set()
    while True:
        variables = {"owner": owner, "name": name, "number": number}
        if cursor is not None:
            variables["cursor"] = cursor
        request = {"kind": "graphql", "query": THREADS, "number": number, "cursor": cursor}
        data = query(transport, limiter, sink, THREADS, variables, request)
        connection = dig(data, "repository", "pullRequest", "reviewThreads")
        if not isinstance(connection, dict):
            if data is not None:
                sink.miss(request, None, "no_review_threads")
            return
        for thread in connection.get("nodes") or []:
            comments = dig(thread, "comments", "pageInfo")
            thread_id = dig(thread, "id")
            more, broken = _next_cursor(comments, set())
            if broken or (more is not None and not isinstance(thread_id, str)):
                sink.miss(request, None, "unusable_cursor")
            elif more is not None:
                walk_comments(transport, limiter, sink, thread_id, more, number)
        cursor, broken = _next_cursor(connection.get("pageInfo"), seen)
        if broken:
            sink.miss(request, None, "unusable_cursor")
        if cursor is None:
            return
