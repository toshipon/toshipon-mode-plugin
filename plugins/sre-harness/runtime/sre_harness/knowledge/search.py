"""`knowledge search`: the approved norms that apply to one piece of work, within a budget.

No model, no network, no cache: the same request on the same generation at the same clock gives
the same bytes. Every record handed in is one the principal may read (the store's access
filter), so nothing counted or listed here can be about a record the principal cannot read.

    1. resolve the principal (unknown or no config: exit 3)
    2. keep approved records whose scope is the organization or the given project, component
       and task chain, and whose repo, path and resource type facets fit the request
    3. the mandatory set: the mandatory records among them; it never depends on the query,
       the topics or the decision id
    4. mandatory records over --max-tokens: stop with mandatory_overflow and no records
    5. rank the rest: scope depth, path and resource type matches, bm25, confidence, newer
       update, then the id
    6. fill the budget in that order; the first record that does not fit and all after it
       are counted in `truncated`
    7. report conflicts among the returned records, without settling them
    8. mark how fresh each returned record is

Rules this module decides (the design leaves them open):

    replaced      a record is replaced when a readable successor chain (`supersedes` edges,
                  through approved or superseded records) ends in an approved record, and
                  that successor applies to the request (it passes step 2 itself). Only such
                  ends are returned, whatever status the source gave the older record. A
                  mandatory record is replaced only by a mandatory end: one whose chains end
                  only in advisory records stays in the mandatory set, and the pair is
                  reported in `conflicts` (mandatory_superseded_by_advisory), unsettled.
    same subject  two returned records share a subject when they have the same
                  `facets.decision_id`, or the same scope (level and ids) and the same topics
    freshness     fresh: the record's source last succeeded within --stale-after-hours (a
                  partial update only vouches for the records it wrote, so `indexed_at`
                  counts there); stale: longer ago; unknown: no successful update is on
                  record, or the clock is behind it. Nothing is read from the source here.
    stale_index   a source was last taken in with another knowledge/config.json than the one
                  in place now, so who may read what (and which readers were rejected) may have
                  changed under it. No record is returned until every source named in
                  `stale_sources` is taken in again (`knowledge update`).
    tokens        a returned record costs terms.estimate_tokens of its canonical JSON, exactly
                  as it appears in `records` or `proposed` (see `tokens`). `retrieval` is not
                  counted.
    expansion     words the caller adds to the query (`--expand`): their search terms join the
                  query's, matched with the same OR, so they rank and narrow the advisory
                  records only. They are a set: their order and repetition play no part.
    retrieval     how the advisory records were matched: the method (only "fts5" here), the
                  number of search terms of the query, and of the expansion (null without one).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from .. import records
from ..errors import EXIT_OK, EXIT_QUALITY, BoundaryError, InputError
from ..globs import path_matches
from ..review.markdown import clean
from . import config, model, store, terms, untrusted

DEFAULT_MAX_TOKENS = 1500
STALE_AFTER_HOURS = 168
SECONDS_PER_HOUR = 3600
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
CONFIDENCE = {
    ("review", "strong"): 3,
    ("review", "recurring"): 2,
    ("review", "candidate"): 1,
    ("spec", "high"): 3,
    ("spec", "medium"): 2,
    ("spec", "low"): 1,
}
CHAIN_STATUSES = frozenset({"approved", "superseded"})  # a successor chain runs through these
VOUCHING_STATES = frozenset({"complete", "partial"})  # source states that mean "it succeeded"
SAME_SUBJECT = "both_approved_same_subject"
ADVISORY_SUCCESSOR = "mandatory_superseded_by_advisory"
METHOD = "fts5"
MAX_EXPAND_WORDS = 32
MAX_EXPAND_LENGTH = 64
NOTHING_TRUNCATED = {"count": 0, "ids": [], "tokens_needed": 0}


@dataclass(frozen=True)
class Request:
    project: str | None = None
    component: str | None = None
    task: str | None = None
    query: str = ""
    repo: str | None = None
    paths: tuple = ()
    resource_types: tuple = ()
    topics: tuple = ()
    decision_id: str | None = None
    include_proposed: bool = False
    max_tokens: int = DEFAULT_MAX_TOKENS
    stale_after_hours: int = STALE_AFTER_HOURS
    expand: tuple | None = None  # the caller's expansion words (see `expansion`)


# The query and its expansion ---------------------------------------------------------------


def expansion(words):
    """The caller's expansion words, checked where they come in: at most MAX_EXPAND_WORDS words
    of at most MAX_EXPAND_LENGTH characters (exit 2 otherwise), without control and format
    characters, as a sorted set. FTS5 syntax in them is only words, as in the query."""
    words = list(words)
    if len(words) > MAX_EXPAND_WORDS:
        raise InputError(f"--expand takes at most {MAX_EXPAND_WORDS} words.")
    if any(len(word) > MAX_EXPAND_LENGTH for word in words):
        raise InputError(f"an --expand word has at most {MAX_EXPAND_LENGTH} characters.")
    cleaned = {"".join(c for c in word if not untrusted.invisible(c)).strip() for word in words}
    return tuple(sorted(word for word in cleaned if word))


def _expansion_terms(request):
    return sorted({term for word in request.expand or () for term in terms.terms(word)})


def search_terms(request):
    """The terms the advisory records are matched with: the query's and the expansion's."""
    return sorted(set(terms.terms(request.query)) | set(_expansion_terms(request)))


def retrieval(request):
    """How the advisory records were matched. Never counted against the budget."""
    widened = None
    if request.expand is not None:
        widened = {"source": "caller", "terms": len(_expansion_terms(request))}
    return {
        "method": METHOD,
        "query_terms": len(terms.terms(request.query)),
        "expansion": widened,
    }


# Filtering -------------------------------------------------------------------------------


def _depth(record):
    return model.LEVELS.index(record["scope"]["level"])


def in_scope(record, request):
    """The organization, or a record whose ids down to its level are the ones asked for. A
    level the request does not name matches nothing, so `--project` alone leaves out the
    component and task records."""
    given = (request.project, request.component, request.task)
    scope = record["scope"]
    return all(
        given[position] is not None and scope[name] == given[position]
        for position, name in enumerate(model.SCOPE_IDS[: _depth(record)])
    )


def facet_match(record, request):
    """(applies, path hit, resource type hit). A facet the record does not have, or the request
    does not give, excludes nothing; a hit needs both sides."""
    facets = record["facets"]
    if facets["repo"] and request.repo and facets["repo"] != request.repo:
        return False, False, False
    globs = facets["path_globs"]
    path_hit = any(path_matches(path, globs) for path in request.paths) if globs else False
    if globs and request.paths and not path_hit:
        return False, False, False
    kinds = facets["resource_type"]
    type_hit = bool(set(kinds) & set(request.resource_types))
    if kinds and request.resource_types and not type_hit:
        return False, False, False
    return True, path_hit, type_hit


def _ends(rows):
    """{id: approved ids at the end of its successor chains} for every record of `rows` that
    has one. Successors that are proposed, rejected, retired or unresolved replace nothing."""
    by_id = {row["id"]: row for row in rows}
    successors = {}
    for row in rows:
        for target in row["supersedes"]:
            successors.setdefault(target, []).append(row["id"])
    ends = {}

    def chain_ends(record_id):
        if record_id not in ends:
            found = set()
            for successor in sorted(successors.get(record_id, ())):
                status = by_id[successor]["status"]
                if status not in CHAIN_STATUSES:
                    continue
                further = chain_ends(successor)
                if further:
                    found |= further
                elif status == "approved":
                    found.add(successor)
            ends[record_id] = found
        return ends[record_id]

    return {
        record_id: chain_ends(record_id)
        for record_id in sorted(successors)
        if record_id in by_id and chain_ends(record_id)
    }


def _mandatory_ends(rows, ends):
    """`ends` with the ends that may replace each record: only a mandatory end replaces a
    mandatory record."""
    by_id = {row["id"]: row for row in rows}
    kept = {}
    for record_id, found in ends.items():
        if by_id[record_id]["enforcement"] == "mandatory":
            found = {end for end in found if by_id[end]["enforcement"] == "mandatory"}
        kept[record_id] = found
    return kept


def replacements(rows):
    """{id: approved ids at the end of its successor chains} for every replaced record in
    `rows`: a successor replaces only within the rows it is handed (`narrow` hands it the
    records that apply to the request), and a mandatory record only by a mandatory end."""
    kept = _mandatory_ends(rows, _ends(rows))
    return {record_id: tuple(sorted(found)) for record_id, found in kept.items() if found}


def overruled(rows):
    """(mandatory id, advisory end) for every mandatory record of `rows` whose successor chains
    end only in advisory records: it stays mandatory, and the pair is a conflict to settle."""
    by_id = {row["id"]: row for row in rows}
    ends = _ends(rows)
    kept = _mandatory_ends(rows, ends)
    return sorted(
        (record_id, end)
        for record_id, found in ends.items()
        if by_id[record_id]["enforcement"] == "mandatory" and not kept[record_id]
        for end in found
    )


# Ranking ---------------------------------------------------------------------------------


def confidence_rank(confidence):
    """The review and the spec vocabularies on one scale: 3 high, 2 middle, 1 low, 0 none."""
    if not confidence:
        return 0
    return CONFIDENCE.get((confidence["vocabulary"], confidence["value"]), 0)


def _seconds(timestamp):
    try:
        moment = datetime.strptime(timestamp, TIMESTAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return int(moment.timestamp())


def _rank(record, request, bm25=0.0):
    _, path_hit, type_hit = facet_match(record, request)
    return (
        -_depth(record),
        -(int(path_hit) + int(type_hit)),
        bm25,
        -confidence_rank(record["confidence"]),
        -(_seconds(record["updated_at"]) or 0),
        record["id"],
    )


def reasons(record, request, term_hit=False):
    """why_selected. A mandatory record never carries term_match: its place does not depend on
    the query, and neither do its tokens."""
    _, path_hit, type_hit = facet_match(record, request)
    mandatory = record["enforcement"] == "mandatory"
    found = ["mandatory"] if mandatory else []
    if path_hit:
        found.append("path_match")
    if type_hit:
        found.append("resource_type_match")
    if term_hit and not mandatory:
        found.append("term_match")
    return found or ["scope_match"]


# What is returned ------------------------------------------------------------------------


def view(record, why, freshness):
    """A record as `knowledge search` prints it: normative fields and evidence pointers, no
    source text. Strings that came from a source lose control and bidi characters."""
    scope = record["scope"]
    shown = {"level": scope["level"]}
    shown.update({name: scope[name] for name in model.SCOPE_IDS[: _depth(record)]})
    return {
        "id": record["id"],
        "kind": record["kind"],
        "enforcement": record["enforcement"],
        "status": record["status"],
        "scope": shown,
        "summary": clean(record["summary"]),
        "applies_when": [clean(text) for text in record["applies_when"]],
        "exceptions": [clean(text) for text in record["exceptions"]],
        "confidence": record["confidence"],
        "evidence": [
            {
                "locator": clean(evidence["locator"]),
                "revision": clean(evidence["revision"]),
                "content_sha256": evidence["content_sha256"],
                "size": evidence["size"],
            }
            for evidence in record["evidence"]
        ],
        "freshness": freshness,
        "why_selected": list(why),
    }


def tokens(shown):
    """The one rule for what a returned record costs: its canonical JSON, estimated."""
    return terms.estimate_tokens(records.canonical(shown))


def freshness(record, source, sources, now, stale_after_hours):
    """fresh, stale or unknown, from when the record's source last succeeded. `sources` is
    {name: {state, last_success_at}}."""
    row = sources.get(source)
    if row is None or row["state"] not in VOUCHING_STATES:
        return "unknown"
    vouched = row["last_success_at"] if row["state"] == "complete" else record["indexed_at"]
    start, end = _seconds(vouched), _seconds(now)
    if start is None or end is None or end < start:
        return "unknown"
    return "fresh" if end - start <= stale_after_hours * SECONDS_PER_HOUR else "stale"


def conflicts(selected, conflict_edges, overruled_pairs=()):
    """Groups of returned records that may contradict each other, sorted. All of them are
    approved and none replaces another (a replaced record is never returned). A returned
    mandatory record whose successor is advisory (`overruled`) is paired with that successor,
    whether or not the successor was returned: it is readable, and it says what to settle."""
    chosen = {row["id"] for row in selected}
    groups = {}
    for row in selected:
        facets, scope = row["facets"], row["scope"]
        if facets["decision_id"]:
            groups.setdefault(("decision", facets["decision_id"]), set()).add(row["id"])
        if facets["topic"]:
            where = tuple(scope[name] or "" for name in ("level", *model.SCOPE_IDS))
            groups.setdefault(("topic", where, tuple(facets["topic"])), set()).add(row["id"])
    found = {(SAME_SUBJECT, tuple(sorted(ids))) for ids in groups.values() if len(ids) > 1}
    found |= {
        (store.CONFLICTS_WITH, tuple(sorted((first, second))))
        for first, second in conflict_edges
        if first in chosen and second in chosen and first != second
    }
    found |= {
        (ADVISORY_SUCCESSOR, tuple(sorted((mandatory, successor))))
        for mandatory, successor in overruled_pairs
        if mandatory in chosen
    }
    return [{"ids": list(ids), "reason": reason} for reason, ids in sorted(found)]


def _budget(request, used):
    return {
        "estimator": terms.ESTIMATOR,
        "max_tokens": request.max_tokens,
        "used_tokens": used,
        "task": None,  # the CLI fills in the task total when --task is given
    }


def stopped(status, request, mandatory):
    """The answer when the search stops before choosing anything."""
    return {
        "status": status,
        "budget": _budget(request, 0),
        "mandatory": mandatory,
        "records": [],
        "proposed": [],
        "truncated": dict(NOTHING_TRUNCATED),
        "unfetched": [],
        "conflicts": [],
        "freshness": {"stale": 0, "unknown": 0, "sources_failed": []},
        "superseded": [],
    }


def _superseded(applicable, replaced, request):
    if request.decision_id is None:
        return []
    by = sorted(
        {
            successor
            for row in applicable
            if row["facets"]["decision_id"] == request.decision_id and row["id"] in replaced
            for successor in replaced[row["id"]]
        }
    )
    return [{"asked": clean(request.decision_id), "by": by}] if by else []


@dataclass(frozen=True)
class Narrowed:
    """Step 2. `applicable`: every record in scope whose facets fit, by id; `replaced`: see
    `replacements`, over `applicable` only, so a successor that does not apply to the request
    replaces nothing in it; `live`: the approved records of `applicable` that nothing
    replaces; `overruled`: see `overruled`, over `applicable` too."""

    applicable: list
    replaced: dict
    live: list
    overruled: list


def narrow(rows, request):
    """Step 2 over the records the principal may read. Pure; the query plays no part."""
    applicable = [
        row
        for row in sorted(rows, key=lambda row: row["id"])
        if in_scope(row, request) and facet_match(row, request)[0]
    ]
    replaced = replacements(applicable)
    live = [row for row in applicable if row["status"] == "approved" and row["id"] not in replaced]
    return Narrowed(applicable, replaced, live, overruled(applicable))


def select(rows, *, request, matches, provenance, sources, conflict_edges, now):
    """Steps 2-8 over the records the principal may read. Pure: the store's answers come in as
    plain values (`matches` {id: bm25}, `provenance` {id: source}, `sources` {name: row},
    `conflict_edges` [(id, id)])."""
    words = search_terms(request)
    narrowed = narrow(rows, request)
    applicable, replaced, live = narrowed.applicable, narrowed.replaced, narrowed.live

    def fresh(row):
        return freshness(row, provenance.get(row["id"]), sources, now, request.stale_after_hours)

    must = sorted(
        (row for row in live if row["enforcement"] == "mandatory"),
        key=lambda row: _rank(row, request),
    )
    must_views = [view(row, reasons(row, request), fresh(row)) for row in must]
    must_tokens = sum(tokens(shown) for shown in must_views)
    mandatory = {
        "count": len(must),
        "tokens": must_tokens,
        "complete": must_tokens <= request.max_tokens,
        "ids": sorted(row["id"] for row in must),
    }
    if not mandatory["complete"]:
        return stopped("mandatory_overflow", request, mandatory)

    def wanted(row):
        if request.topics and not set(row["facets"]["topic"]) & set(request.topics):
            return False
        if request.decision_id is not None and row["facets"]["decision_id"] != request.decision_id:
            return False
        return not words or row["id"] in matches

    def ranked(group):
        return sorted(group, key=lambda row: _rank(row, request, matches.get(row["id"], 0.0)))

    queue = ranked(r for r in live if r["enforcement"] != "mandatory" and wanted(r))
    if request.include_proposed:
        pending = (r for r in applicable if r["status"] == "proposed" and r["id"] not in replaced)
        queue += ranked(r for r in pending if wanted(r))

    left = request.max_tokens - must_tokens
    chosen, truncated = [], []
    for row in queue:
        shown = view(row, reasons(row, request, term_hit=bool(words)), fresh(row))
        cost = tokens(shown)
        if not truncated and cost <= left:
            chosen.append((row, shown))
            left -= cost
        else:
            truncated.append((row["id"], cost))

    returned = must_views + [shown for _, shown in chosen]
    failed = {
        provenance.get(shown["id"])
        for shown in returned
        if sources.get(provenance.get(shown["id"]), {}).get("state") not in VOUCHING_STATES
    }
    return {
        "status": "ok",
        "budget": _budget(request, request.max_tokens - left),
        "mandatory": mandatory,
        "records": must_views + [shown for row, shown in chosen if row["status"] == "approved"],
        "proposed": [shown for row, shown in chosen if row["status"] == "proposed"],
        "truncated": {
            "count": len(truncated),
            "ids": [record_id for record_id, _ in truncated],
            "tokens_needed": sum(cost for _, cost in truncated),
        },
        "unfetched": [],
        "conflicts": conflicts(
            must + [row for row, _ in chosen if row["status"] == "approved"],
            conflict_edges,
            narrowed.overruled,
        ),
        "freshness": {
            "stale": sum(shown["freshness"] == "stale" for shown in returned),
            "unknown": sum(shown["freshness"] == "unknown" for shown in returned),
            "sources_failed": sorted(name for name in failed if name),
        },
        "superseded": _superseded(applicable, replaced, request),
    }


def run(root, name, request, now):
    """Step 1 and the store's part: one read transaction, one generation."""
    settings = config.load(root)
    if settings is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    principal = config.principal(settings, name)
    with store.reader(root) as reader:
        meta = reader.meta()
        head = {
            "schema_version": records.SCHEMA_VERSION,
            "status": None,
            "snapshot": {"generation": reader.generation, "updated_at": meta["updated_at"]},
            "principal": principal.name,
            "retrieval": retrieval(request),
        }
        stale = reader.stale_sources(config.config_hash(settings))
        if stale:
            empty = {"count": 0, "tokens": 0, "complete": False, "ids": []}
            return {**head, **stopped("stale_index", request, empty), "stale_sources": stale}
        words = search_terms(request)
        body = select(
            reader.records(principal),
            request=request,
            matches=dict(reader.matching(principal, words)) if words else {},
            provenance=reader.provenance(principal),
            sources={row["name"]: row for row in reader.sources()},
            conflict_edges=reader.edges(principal, store.CONFLICTS_WITH),
            now=now,
        )
    return {**head, **body}


def exit_code(result):
    return EXIT_OK if result["status"] == "ok" else EXIT_QUALITY
