"""REST over `gh api -i`, one page per call. The status line and headers are read here.

The transport never follows a URL the server returned. From a `Link` header it keeps only the
page number, and the next request is built again from the template in `guard.REST_ENDPOINTS`.
"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .. import guard
from ..errors import QualityGateError

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RETRIES = 3
SECONDARY_DEFAULT_WAIT = 60  # GitHub asks for at least a minute when retry-after is absent
KEPT_HEADERS = (
    "content-type",
    "link",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-resource",
    "x-ratelimit-used",
)

_STATUS = re.compile(r"HTTP/[0-9.]{1,3} ([1-5][0-9]{2})(?: [^\r\n]*)?")
_HEADER = re.compile(r"([A-Za-z0-9-]{1,100}):[ \t]*(.*)")
_LINK = re.compile(r'<([^<>]*)>\s*;\s*rel="([a-z]{1,20})"')
_PAGE = re.compile(r"(?:^|&)page=([1-9][0-9]{0,5})(?:&|#|$)")
_EPOCH = re.compile(r"[0-9]{1,12}")


class ResponseTooLarge(QualityGateError):
    reason = "response_too_large"


class WaitLimitExceeded(QualityGateError):
    reason = "wait_limit"


@dataclass(frozen=True)
class Response:
    """One HTTP response. `status` is None when gh printed no HTTP response at all."""

    status: Optional[int]
    headers: dict = field(default_factory=dict)
    body: str = ""
    exit_code: int = 0

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 300

    def data(self):
        """The JSON body. Raises ValueError when the body is not JSON."""
        return json.loads(self.body)

    def kept_headers(self):
        return {name: self.headers[name] for name in KEPT_HEADERS if name in self.headers}


def _split_head(text):
    """Header block and body, split at the first empty line (CRLF or LF)."""
    match = re.search(r"\r?\n\r?\n", text)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.end() :]


def parse_response(text, exit_code=0):
    """Read `gh api -i` output: a status line, headers, an empty line, then the body."""
    text = text or ""
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge(
            f"a response was larger than {MAX_RESPONSE_BYTES} bytes; "
            "collection stopped instead of truncating it."
        )
    head, body = _split_head(text)
    lines = [line.rstrip("\r") for line in head.split("\n")]
    status = _STATUS.fullmatch(lines[0]) if lines else None
    if status is None:
        return Response(None, {}, "", exit_code)
    headers = {}
    for line in lines[1:]:
        header = _HEADER.fullmatch(line)
        if header is None:
            continue
        name, value = header.group(1).lower(), header.group(2).strip()
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return Response(int(status.group(1)), headers, body, exit_code)


def link_pages(value):
    """`{rel: page number}` from a Link header. The URL itself is dropped."""
    pages = {}
    for url, rel in _LINK.findall(value or ""):
        page = _PAGE.search(url.partition("?")[2])
        if page is not None:
            pages[rel] = int(page.group(1))
    return pages


class GhTransport:
    """The real transport. Every request goes through `guard.gh_rest` or `guard.gh_graphql`."""

    def __init__(self, token, host=None, env=None, timeout=60):
        self._token = token
        self._host = host
        self._env = env
        self._timeout = timeout

    def __repr__(self):
        return f"GhTransport(host={self._host!r})"  # the token never appears

    def rest(self, endpoint, values, query):
        code, out = guard.gh_rest(
            endpoint,
            values,
            query,
            token=self._token,
            host=self._host,
            env=self._env,
            timeout=self._timeout,
        )
        return parse_response(out, code)

    def graphql(self, name, variables):
        code, out = guard.gh_graphql(
            name,
            variables,
            token=self._token,
            host=self._host,
            env=self._env,
            timeout=self._timeout,
        )
        return parse_response(out, code)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value):
    return int(value) if isinstance(value, str) and _EPOCH.fullmatch(value) else None


class Limiter:
    """Waits that rate limits ask for, with a cap on the total. Clock and sleep are injected."""

    def __init__(self, clock, sleep, max_wait):
        self.clock = clock
        self.sleep = sleep
        self.max_wait = max_wait
        self.waited = 0
        self.waits = []
        self._resume_at = None
        self._reason = None

    def hold_until(self, epoch, reason):
        if self._resume_at is None or epoch > self._resume_at:
            self._resume_at, self._reason = epoch, reason

    def before(self, request):
        """Called before every request: wait out a hold set by an earlier response."""
        if self._resume_at is None:
            return
        delay = math.ceil(self._resume_at - self.clock())
        reason, self._resume_at, self._reason = self._reason, None, None
        if delay <= 0:
            return
        if self.waited + delay > self.max_wait:
            raise WaitLimitExceeded(
                f"rate limit asked for {delay}s more after {self.waited}s; "
                f"the cap is {self.max_wait}s (--max-wait)."
            )
        self.waits.append(
            {"reason": reason, "seconds": delay, "at": _iso(self.clock()), "request": request}
        )
        self.sleep(delay)
        self.waited += delay

    def observe(self, response):
        """Read the limit headers. True when the request was refused by a limit and may retry."""
        headers = response.headers
        now = self.clock()
        reset = _epoch(headers.get("x-ratelimit-reset"))
        exhausted = headers.get("x-ratelimit-remaining") == "0"
        if response.status in (403, 429) and _rate_limited(response, exhausted):
            retry_after = _epoch(headers.get("retry-after"))
            if retry_after is not None:
                self.hold_until(now + retry_after, "secondary")
            elif exhausted and reset is not None:
                self.hold_until(reset, "primary")
            else:
                self.hold_until(now + SECONDARY_DEFAULT_WAIT, "secondary")
            return True
        if exhausted and reset is not None:
            self.hold_until(reset, "primary")
        return False

    def record(self):
        return {
            "waits": self.waits,
            "waited_seconds": self.waited,
            "max_wait_seconds": self.max_wait,
        }


def _rate_limited(response, exhausted):
    if response.status == 429 or exhausted or "retry-after" in response.headers:
        return True
    return "rate limit" in response.body[:2000].lower()


def fetch(limiter, request, call, limited: Optional[Callable] = None) -> Response:
    """One logical request, retried while a rate limit refuses it. Returns the last response."""
    attempt = 0
    while True:
        limiter.before(request)
        response = call()
        refused = limiter.observe(response) or (limited is not None and limited(response))
        if not refused or attempt >= MAX_RETRIES:
            return response
        attempt += 1


def failure_reason(response):
    """Why a response cannot be used, or None when it can."""
    if response.status is None:
        return "no_http_response"
    if response.status in (403, 429) and _rate_limited(
        response, response.headers.get("x-ratelimit-remaining") == "0"
    ):
        return "rate_limited"
    if not response.ok:
        return f"http_{response.status}"
    return None
