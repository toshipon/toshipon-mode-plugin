"""The reference decision queue: files under <state root>/knowledge/decisions/, no other tool.

A decision queue adapter offers two operations, and this module is one adapter among the ones
a user may write for another queue:

    put(request)          -> accepted | duplicate (with the request already there)
    poll(workspace_id)    -> the answers for that workspace, each delivered once

    decisions/queue.lock                       held (flock) while put, answer, cancel and poll
                                               decide and write
    decisions/outbox/<id>.json                 a request (decision-request), status open
    decisions/inbox/<id>.json                  its answer (decision-response)
    decisions/cancelled/<id>.json              {request_id, reason, cancelled_at}
    decisions/waiters/<id>/<workspace>.json    a workspace that put the request
    decisions/delivered/<id>/<workspace>.json  {request_id, workspace_id, delivered_at,
                                               response_sha256}: the answer reached it

Every file is written once: it is made complete beside its place and linked in, so it appears
whole or not at all and an existing one is never replaced. Directories are 0700, files 0600,
and a symlink anywhere in the queue is refused (exit 3).

Rules this module decides:

    dedupe key   sha256 of [task, normalized question, sorted evidence ids, revision]. A put
                 whose key matches a request that is not cancelled or expired is a duplicate
                 of it, from whichever workspace; that workspace becomes one of its waiters.
    revision     the index generation, {evidence id: content_hash} read with the putting
                 principal's access, and --head-sha. Only the content hashes and the head
                 decide staleness; a generation that moved for other records does not.
    subject      [task, normalized question, sorted evidence ids]. A request is superseded by
                 a later request of the same subject that is not cancelled (later: higher
                 generation, then created_at, then id).
    status       cancelled, else superseded, else answered, else expired (now is at or past
                 expires_at), else open. The outbox file always says open; this is derived.
    stale        an answer about to be delivered whose evidence no longer has the recorded
                 content hash (content_hash_changed) or cannot be read with the request's
                 principal any more (evidence_unavailable), or whose head differs from
                 --head-sha (head_sha_changed) or is not given (head_sha_not_given). It is not
                 delivered, and poll exits 1.
    delivered    a workspace gets an answer once; after that it is `already_delivered` and is
                 not checked for staleness again (it was applied already).
    reuse        task by default. An answer reused beyond its task is returned as a
                 `candidate_hint` for a person to take through the approval flow; nothing here
                 writes the index or the registry, and an answer never becomes a rule.

A responder must name a person: AI and automation names are refused, as autonomous-improvement
refuses them for reviewers (the same words, the same splitting), and so is a run marked as
automated. It is not authentication.
"""

import fcntl
import json
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from .. import paths, privacy, records
from ..errors import BoundaryError, InputError, QualityGateError
from ..review.markdown import escape
from . import config, store, untrusted

DIRECTORY = "decisions"
LOCK = "queue.lock"
OUTBOX, INBOX, CANCELLED = "outbox", "inbox", "cancelled"
WAITERS, DELIVERED = "waiters", "delivered"
REQUEST_ID = re.compile(r"DR-[0-9a-f]{12}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
QUESTION_LENGTH, OPTION_LENGTH = 500, 100
MIN_OPTIONS, MAX_OPTIONS, MAX_EVIDENCE = 2, 10, 20
RESPONDER_LENGTH, REASON_LENGTH = 100, 200
CONFIRM_LENGTH = 12
MAX_FILE_BYTES = 64 * 1024
RISKS = ("low", "medium", "high")
REUSE_SCOPES = ("task", "project")
LIVE = frozenset({"open", "answered", "superseded"})  # a put with the same key joins these
# autonomous-improvement's human gate (ai_harness/store.py), kept here: the plugins install apart.
RESERVED_NAMES = ("claude", "ai", "bot", "agent", "assistant", "harness", "gpt", "llm")
AUTOMATED_ENV, ACTIVE_ENV = "AI_HARNESS_AUTOMATED", "AI_HARNESS_ACTIVE"
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_A-Za-z]+")


# Files -----------------------------------------------------------------------------------


def _folder(root, *parts, create=True):
    """<root>/knowledge/decisions/<parts>, each level 0700 and no symlink (exit 3)."""
    path = store.directory(root, create=create)
    for part in (DIRECTORY, *parts):
        path = paths.child(path, part)
        if path.is_symlink():
            raise BoundaryError(f"refused to use the decision queue through a symlink: {path}")
        if create and not path.exists():
            try:
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
            except FileExistsError:
                pass
        if path.exists() and not path.is_dir():
            raise BoundaryError(f"refused to use {path} for the decision queue: not a directory.")
    return path


def _create(folder, name, value):
    """Write `value` as <folder>/<name> once. FileExistsError when it is there already."""
    target = paths.child(folder, name)
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write((records.canonical(value) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, target)  # fails when the target exists: never replaced
    finally:
        os.unlink(temporary)
    return target


def _read(path, schema=None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BoundaryError(f"refused to read the queue file {path}: {error.strerror}") from None
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    try:
        value = json.loads(data.decode("utf-8")) if len(data) <= MAX_FILE_BYTES else None
    except (UnicodeDecodeError, ValueError):
        value = None
    if value is None or (schema and records.violations(schema, value)):
        raise InputError(f"the queue file {path} is damaged; nothing was changed.")
    return value


def _json_files(folder):
    if not folder.is_dir():
        return []
    return [
        path
        for path in sorted(folder.iterdir())
        if path.suffix == ".json" and not path.name.startswith(".")
    ]


@contextmanager
def _locked(root):
    folder = _folder(root)
    path = paths.child(folder, LOCK)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as error:
        raise BoundaryError(f"refused to open the queue lock {path}: {error.strerror}") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fchmod(descriptor, 0o600)
        yield folder
    finally:
        os.close(descriptor)  # closing releases the lock


@dataclass
class Queue:
    """Everything under decisions/, read at one moment."""

    requests: dict = field(default_factory=dict)  # id -> request
    responses: dict = field(default_factory=dict)  # id -> response
    cancelled: dict = field(default_factory=dict)  # id -> cancellation
    waiters: dict = field(default_factory=dict)  # id -> {workspace}
    delivered: dict = field(default_factory=dict)  # id -> {workspace: delivery}


def _per_workspace(folder):
    found = {}
    if not folder.is_dir():
        return found
    for entry in sorted(folder.iterdir()):
        if not REQUEST_ID.fullmatch(entry.name):
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise BoundaryError(f"refused to read the queue through {entry}: not a directory.")
        found[entry.name] = {
            path.stem: _read(path) for path in _json_files(entry) if NAME.fullmatch(path.stem)
        }
    return found


def load(root):
    """The queue as it is now. Without a queue, an empty one (nothing is created)."""
    base = _folder(root, create=False)
    queue = Queue()
    if not base.is_dir():
        return queue
    for name, kept, schema in (
        (OUTBOX, queue.requests, "decision-request"),
        (INBOX, queue.responses, "decision-response"),
        (CANCELLED, queue.cancelled, None),
    ):
        folder = _folder(root, name, create=False)
        for path in _json_files(folder):
            if REQUEST_ID.fullmatch(path.stem):
                value = _read(path, schema)
                if value.get("id", value.get("request_id")) != path.stem:
                    raise InputError(f"the queue file {path} names another request.")
                kept[path.stem] = value
    queue.waiters = {key: set(rows) for key, rows in _per_workspace(base / WAITERS).items()}
    queue.delivered = _per_workspace(base / DELIVERED)
    return queue


# Values from the command line --------------------------------------------------------------


def text(value, limit, what):
    """One line: control and bidi characters dropped, whitespace collapsed (exit 2 when it is
    empty or longer than `limit`)."""
    cleaned = " ".join(untrusted.clean(value).split())
    if not 1 <= len(cleaned) <= limit:
        raise InputError(f"{what} must be 1-{limit} characters on one line.")
    return cleaned


def normalized(question):
    return " ".join(unicodedata.normalize("NFKC", question).casefold().split())


def timestamp(value, what):
    try:
        datetime.strptime(value, TIMESTAMP)
    except (TypeError, ValueError):
        raise InputError(f"{what} must be a UTC time like 2026-11-27T00:00:00Z.") from None
    return value


def _future(value, now, what):
    if timestamp(value, what) <= now:
        raise InputError(f"{what} must be later than now ({now}).")
    return value


def require_person(responder, env=None):
    """The responder, when it names a person; AI and automation names are refused (exit 3)."""
    env = os.environ if env is None else env
    if env.get(AUTOMATED_ENV) or env.get(ACTIVE_ENV):
        raise BoundaryError("an answer cannot be recorded from an automated run.")
    name = text(responder, RESPONDER_LENGTH, "--responder")
    words = [word.lower() for word in _WORDS.findall(name)]
    if any(word in RESERVED_NAMES for word in words):
        raise BoundaryError("--responder must name a person; AI and automation names are refused.")
    return name


def reason_text(reason):
    value = text(reason, REASON_LENGTH, "--reason")
    if privacy.find_secrets(value):
        raise InputError("--reason has the shape of a secret; it was not recorded.")
    return value


# Status ------------------------------------------------------------------------------------


def subject(request):
    return records.fingerprint(
        [request["task_id"], normalized(request["question"]), sorted(request["evidence_ids"])]
    )


def _order(request):
    return (request["revision"]["generation"], request["created_at"], request["id"])


def replaced(queue):
    """Ids of the requests a later request of the same subject supersedes."""
    latest = {}
    for request in queue.requests.values():
        if request["id"] in queue.cancelled:
            continue
        key = subject(request)
        if key not in latest or _order(request) > _order(latest[key]):
            latest[key] = request
    return {
        request["id"]
        for request in queue.requests.values()
        if subject(request) in latest and _order(request) < _order(latest[subject(request)])
    }


def status(request, queue, now, superseded):
    request_id = request["id"]
    if request_id in queue.cancelled:
        return "cancelled"
    if request_id in superseded:
        return "superseded"
    if request_id in queue.responses:
        return "answered"
    if request.get("expires_at") and now >= request["expires_at"]:
        return "expired"
    return "open"


def view(request, queue, now, superseded):
    return {**request, "status": status(request, queue, now, superseded)}


# put ---------------------------------------------------------------------------------------


def _checked(question, options, evidence_ids, risk):
    question = text(question, QUESTION_LENGTH, "--question")
    options = [text(option, OPTION_LENGTH, "--option") for option in options]
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS or len(set(options)) != len(options):
        raise InputError(f"give {MIN_OPTIONS}-{MAX_OPTIONS} different --option values.")
    if privacy.find_secrets("\n".join([question, *options])):
        raise InputError("the question or an option has the shape of a secret; nothing was put.")
    evidence = sorted(set(evidence_ids))
    if not 1 <= len(evidence) <= MAX_EVIDENCE:
        raise InputError(f"give 1-{MAX_EVIDENCE} --evidence record ids.")
    if risk not in RISKS:
        raise InputError(f"--risk must be one of {', '.join(RISKS)}.")
    return question, options, evidence


def _revision(root, task, evidence, head_sha):
    """The state the question is asked in, read with the task principal's access. An id the
    principal cannot read and one that does not exist get the same answer."""
    settings = config.load(root)
    if settings is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    principal = config.principal(settings, task["principal"])
    with store.reader(root) as reader:
        if reader.meta()["config_hash"] != config.config_hash(settings):
            raise QualityGateError(
                "the knowledge index was built with another knowledge/config.json; run "
                "`sre-harness knowledge update` first. Nothing was put."
            )
        found = {row["id"]: row["content_hash"] for row in reader.records(principal, evidence)}
        generation = reader.generation
    for record_id in evidence:
        if record_id not in found:
            raise InputError(
                f"evidence {record_id} is not a record {principal.name} can read; nothing was put."
            )
    return {"generation": generation, "content_hashes": found, "head_sha": head_sha}


def _wait(root, request_id, workspace, now):
    folder = _folder(root, WAITERS, request_id)
    try:
        _create(
            folder,
            f"{workspace}.json",
            {"request_id": request_id, "workspace_id": workspace, "at": now},
        )
    except FileExistsError:
        pass


def put(root, task, *, question, options, evidence_ids, risk, now, head_sha=None, expires_at=None):
    """Put one request for `task` (a checked task-context). Returns {status, request}."""
    question, options, evidence = _checked(question, options, evidence_ids, risk)
    if expires_at is not None:
        _future(expires_at, now, "--expires-at")
    revision = _revision(root, task, evidence, head_sha)
    key = records.fingerprint([task["task_id"], normalized(question), evidence, revision])
    workspace = task["workspace_id"]
    with _locked(root):
        queue = load(root)
        superseded = replaced(queue)
        same = [r for r in queue.requests.values() if r["dedupe_key"] == key]
        joined = [r for r in same if status(r, queue, now, superseded) in LIVE]
        if joined:
            request = min(joined, key=_order)
            _wait(root, request["id"], workspace, now)
            queue.waiters.setdefault(request["id"], set()).add(workspace)
            shown = view(request, queue, now, superseded)
            return {
                "schema_version": records.SCHEMA_VERSION,
                "status": "duplicate",
                "request": shown,
            }
        request = {
            "schema_version": records.SCHEMA_VERSION,
            "id": "DR-" + records.sha256(records.canonical([key, len(same)]))[:12],
            "task_id": task["task_id"],
            "workspace_id": workspace,
            "principal": task["principal"],
            "question": question,
            "options": options,
            "evidence_ids": evidence,
            "risk": risk,
            "status": "open",
            "revision": revision,
            "dedupe_key": key,
            "created_at": now,
        }
        if expires_at is not None:
            request["expires_at"] = expires_at
        records.validate("decision-request", request)
        _create(_folder(root, OUTBOX), f"{request['id']}.json", request)
        _wait(root, request["id"], workspace, now)
    return {"schema_version": records.SCHEMA_VERSION, "status": "accepted", "request": request}


# answer ------------------------------------------------------------------------------------


def reuse_of(scope, until, now):
    scope = scope or "task"
    if scope not in REUSE_SCOPES:
        raise InputError(f"--reuse-scope must be one of {', '.join(REUSE_SCOPES)}.")
    if until is not None:
        _future(until, now, "--reuse-until")
    elif scope != "task":
        raise InputError("--reuse-scope project needs --reuse-until: reuse beyond a task ends.")
    return {"scope": scope, "until": until}


def confirmation(request, option, reuse):
    """What the person confirms: this request, this option, this reuse."""
    return records.fingerprint(
        {"request": records.fingerprint(request), "option": option, "reuse": reuse}
    )


def screen(request, option, reuse, digest):
    revision = request["revision"]
    lines = [
        f"# Answer {request['id']} (task {escape(request['task_id'])}, risk {request['risk']})",
        "",
        f"- question: {escape(request['question'])}",
        "- options:",
        *[f"  - {escape(choice)}" for choice in request["options"]],
        f"- chosen: {escape(option)}",
        f"- evidence: {', '.join(request['evidence_ids'])}",
        f"- revision: generation {revision['generation']}, head {revision['head_sha'] or 'none'}",
        f"- reuse: {reuse['scope']}" + (f" until {reuse['until']}" if reuse["until"] else ""),
        f"- confirmation_sha256: `{digest}`",
        "",
        "An answer applies to its task. Reuse beyond the task is only a hint for a candidate,",
        "which a person takes through `review` to approval; it never becomes a rule here.",
    ]
    return "\n".join(lines)


def _open_request(queue, request_id, now):
    request = queue.requests.get(request_id)
    if request is None:
        raise InputError(f"there is no decision request {request_id}.")
    state = status(request, queue, now, replaced(queue))
    if state != "open":
        raise QualityGateError(f"{request_id} is {state}; nothing was recorded.")
    return request


def answer(root, request_id, *, option, responder, reuse, now, ask, show, env=None):
    """Show what would be recorded, ask for the confirmation prefix, then record the answer
    once. `ask` and `show` are the terminal's; the command line passes them only at a TTY."""
    responder = require_person(responder, env)
    option = text(option, OPTION_LENGTH, "--option")
    queue = load(root)
    request = _open_request(queue, request_id, now)
    if option not in request["options"]:
        raise InputError(f"--option must be one of the options of {request_id}.")
    digest = confirmation(request, option, reuse)
    show(screen(request, option, reuse, digest))
    typed = ask(f"Type the first {CONFIRM_LENGTH} characters of confirmation_sha256 to answer: ")
    if not isinstance(typed, str) or typed.strip() != digest[:CONFIRM_LENGTH]:
        raise BoundaryError("the typed hash does not match the answer; nothing was recorded.")
    response = {
        "schema_version": records.SCHEMA_VERSION,
        "request_id": request_id,
        "option": option,
        "responder": responder,
        "answered_at": now,
        "reuse": reuse,
    }
    records.validate("decision-response", response)
    with _locked(root):
        current = _open_request(load(root), request_id, now)
        if current != request:
            raise QualityGateError(
                f"{request_id} changed while it was shown; nothing was recorded."
            )
        try:
            _create(_folder(root, INBOX), f"{request_id}.json", response)
        except FileExistsError:
            raise QualityGateError(
                f"{request_id} is answered already; nothing was recorded."
            ) from None
    return response


# poll --------------------------------------------------------------------------------------


def _current_hashes(root, requests):
    """(generation, {principal: {id: content_hash}}) of the evidence of `requests`, each read
    with its request's principal. A principal no longer in the config reads nothing."""
    settings = config.load(root)
    if settings is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    wanted = {}
    for request in requests:
        wanted.setdefault(request["principal"], set()).update(request["evidence_ids"])
    found = {}
    with store.reader(root) as reader:
        for name, ids in sorted(wanted.items()):
            if name not in settings["principals"]:
                found[name] = {}
                continue
            principal = config.principal(settings, name)
            found[name] = {row["id"]: row["content_hash"] for row in reader.records(principal, ids)}
        return reader.generation, found


def stale_reasons(request, hashes, head_sha):
    """Why an answer to `request` must not be applied now; empty when it may."""
    reasons = []
    recorded = request["revision"]["content_hashes"]
    if any(record_id not in hashes for record_id in recorded):
        reasons.append("evidence_unavailable")
    elif any(hashes[record_id] != digest for record_id, digest in recorded.items()):
        reasons.append("content_hash_changed")
    head = request["revision"]["head_sha"]
    if head is not None and head_sha is None:
        reasons.append("head_sha_not_given")
    elif head is not None and head != head_sha:
        reasons.append("head_sha_changed")
    return reasons


def poll(root, workspace, *, now, task_id=None, head_sha=None):
    """The requests `workspace` put, and the answers to apply now (each delivered once)."""
    with _locked(root):
        queue = load(root)
        superseded = replaced(queue)
        mine = sorted(
            (
                request
                for request in queue.requests.values()
                if workspace in queue.waiters.get(request["id"], ())
                and (task_id is None or request["task_id"] == task_id)
            ),
            key=lambda request: (request["created_at"], request["id"]),
        )
        answered = [r for r in mine if status(r, queue, now, superseded) == "answered"]
        generation, hashes = _current_hashes(root, answered)
        result = {
            "schema_version": records.SCHEMA_VERSION,
            "status": "ok",
            "workspace_id": workspace,
            "snapshot": {"generation": generation},
            "now": now,
            "requests": [view(request, queue, now, superseded) for request in mine],
            "responses": [],
            "already_delivered": [],
            "stale": [],
            "candidate_hints": [],
        }
        for request in answered:
            request_id, response = request["id"], queue.responses[request["id"]]
            earlier = queue.delivered.get(request_id, {}).get(workspace)
            if earlier is not None:
                result["already_delivered"].append(
                    {
                        "request_id": request_id,
                        "delivered_at": earlier["delivered_at"],
                        "response": response,
                    }
                )
                continue
            reasons = stale_reasons(request, hashes[request["principal"]], head_sha)
            if reasons:
                result["stale"].append({"request_id": request_id, "reasons": reasons})
                continue
            delivery = {
                "request_id": request_id,
                "workspace_id": workspace,
                "delivered_at": now,
                "response_sha256": records.fingerprint(response),
            }
            _create(_folder(root, DELIVERED, request_id), f"{workspace}.json", delivery)
            result["responses"].append({"request_id": request_id, "response": response})
            if response["reuse"]["scope"] != "task":
                result["candidate_hints"].append(
                    {
                        "request_id": request_id,
                        "question": request["question"],
                        "option": response["option"],
                        "evidence_ids": request["evidence_ids"],
                        "reuse": response["reuse"],
                    }
                )
    if result["stale"]:
        result["status"] = "stale_response"
    return result


# list and cancel ---------------------------------------------------------------------------


def listing(root, now, wanted=None):
    """Every request with its status now, oldest first; only `wanted` when given."""
    queue = load(root)
    superseded = replaced(queue)
    shown = [
        view(request, queue, now, superseded)
        for request in sorted(queue.requests.values(), key=lambda r: (r["created_at"], r["id"]))
    ]
    return {
        "schema_version": records.SCHEMA_VERSION,
        "now": now,
        "requests": [row for row in shown if wanted is None or row["status"] == wanted],
    }


def cancel(root, request_id, reason, now):
    """Withdraw an open, expired or superseded request. An answered one stays answered."""
    reason = reason_text(reason)
    with _locked(root):
        queue = load(root)
        request = queue.requests.get(request_id)
        if request is None:
            raise InputError(f"there is no decision request {request_id}.")
        state = status(request, queue, now, replaced(queue))
        if state in {"cancelled", "answered"}:
            raise QualityGateError(f"{request_id} is {state}; nothing was changed.")
        cancellation = {"request_id": request_id, "reason": reason, "cancelled_at": now}
        try:
            _create(_folder(root, CANCELLED), f"{request_id}.json", cancellation)
        except FileExistsError:
            raise QualityGateError(f"{request_id} is cancelled already.") from None
        queue.cancelled[request_id] = cancellation
        return {
            "schema_version": records.SCHEMA_VERSION,
            "request": view(request, queue, now, replaced(queue)),
        }
