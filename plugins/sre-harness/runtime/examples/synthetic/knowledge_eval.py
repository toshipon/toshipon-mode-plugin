"""Evaluate the knowledge index on the synthetic corpus: full load against search + fetch.

No model and no network. Every number comes from the synthetic corpus (`knowledge_corpus`) at
50, 300 or 2000 records, its 30 tasks and its two principals, with fixed clocks and the corpus
seed, so the same arguments always print the same bytes. No time is measured.

**What this shows and what it does not.** The labels come from the generator's own rules, so
the numbers check that the mechanism works on data built to exercise it. They say nothing about
how well a real query is answered, and nothing about a Claude Code usage limit: only the input
tokens of the knowledge itself are estimated, never a session's whole consumption. No single
ratio is a result: the full load grows with the number of records while a search stops at its
budget, so any reduction ratio is set by the size of the corpus.

Definitions (README, "評価"):

    full load      every approved record of the organization and of the task's project that the
                   task's principal may read, loaded whole: the source text each record's
                   evidence points at (a rule's artifact, a decision's or requirement's section),
                   estimated at 3 UTF-8 bytes per token. A source that is gone counts nothing
    search + fetch what the documented procedure returns, by fixed rules standing in for an
                   agent:
                     1. one search as `knowledge context` makes it from the task (project,
                        component, task, changed path, resource type, the task's query),
                        --max-tokens 1500, counted against the task total
                     2. when it stops with mandatory_overflow, one search again with
                        --max-tokens raised to the mandatory tokens + 1500 (what the stop asks
                        for; the person's confirmation is assumed)
                     3. for each returned record, in order: fetch it (--max-tokens 3000, one
                        page, counted against the task total) when one of its applies_when
                        sentences shares a search term with the task's query. That stands in
                        for "read applies_when, fetch only when it applies to this work"
                     4. stop fetching at task_budget_exhausted; nothing is extended
                   The tokens are what the harness counts: `budget.used_tokens` of each search
                   and fetch (the JSON around the records is not counted)
    relevant       the task's mandatory labels and its applicable labels on the task's topic
                   (the topic whose wording is the task's literal query)
    precision      returned and relevant / returned, pooled over the tasks of a group
    recall         returned and relevant / relevant, pooled over the tasks of a group
    groups         tasks whose query uses the records' wording; tasks whose query is a
                   paraphrase sharing no term with it; and the paraphrase tasks searched again
                   with the literal wording (not counted against any total), to show the gap

The search a method makes is a function `(root, principal name, search.Request, now) -> result`
in METHODS; another method (a query widened with synonyms, a hybrid with vectors) is one more
entry, and every metric is computed for it in the same way (`--method`).

**Paraphrase cases** (`--paraphrase`). The cases of `knowledge_corpus.paraphrase_scenario`
(36 queries in the categories ja, en and cross, at least 10 each) are searched as
`knowledge context` searches a task, once per way of querying (PARAPHRASE_PLANS):

    fts5-paraphrase   the paraphrased query as it is
    fts5-literal      the records' own wording as the query: the ceiling a better query reaches
    caller-expansion  the paraphrased query with `--expand` words read from an expansions file
                      ({"model": ..., "run": N, "terms": {case id: [word, ...]}}); one result per
                      file, and the mean, min, max and sample standard deviation over the files.
                      No file is written here: no model is called

    relevant   the case's `relevant` labels: approved advisory records of its topic that apply
               to it. Mandatory records are left out of both sides: they are returned whatever
               the query, so they say nothing about it
    returned   the advisory records the search returned (a search that stops returns none and
               is counted in `stopped`; an expansion the search refuses, in `refused`)
    recall     returned and relevant / relevant, pooled over the cases of a category
    precision  returned and relevant / returned, pooled likewise (none when nothing returned)

each at --max-tokens 1500 (the default) and 20000, where the budget no longer caps the recall.
`--write-expansion-input PATH` writes what a caller that widens queries is given: the case ids
and the paraphrased queries only, never the wording, the labels or a record.

Run it from `runtime/`:

    python -m examples.synthetic.knowledge_eval --size 50 --format markdown
    python -m examples.synthetic.knowledge_eval --size 50 --size 300 --size 2000 > eval.json
    python -m examples.synthetic.knowledge_eval --size 50 --format evaluator-output
    python -m examples.synthetic.knowledge_eval --paraphrase --size 50 --format markdown
    python -m examples.synthetic.knowledge_eval --paraphrase --expansions run1.json \\
        --expansions run2.json
    python -m examples.synthetic.knowledge_eval --write-expansion-input cases.json

The state root is a new temporary directory (outside any repository), removed at the end.
"""

import argparse
import dataclasses
import json
import math
import shutil
import statistics
import tempfile
import threading
from pathlib import Path

from examples.synthetic import github_responses as gh
from examples.synthetic import knowledge_corpus as kc
from examples.synthetic import review_registry as rr
from sre_harness import paths, records
from sre_harness.errors import InputError, QualityGateError
from sre_harness.knowledge import (
    budget,
    check,
    config,
    context,
    decisions,
    fetch,
    ingest,
    model,
    search,
    store,
    terms,
)
from sre_harness.knowledge.adapters import build

INDEXED = "2026-11-27T00:00:00Z"
SEARCHED = "2026-11-27T06:00:00Z"
FETCH_TOKENS = fetch.DEFAULT_MAX_TOKENS
METHODS = {"fts5": search.run}
TOPIC_OF_WORDING = {wording: topic for topic, (wording, _, _) in kc.TOPICS.items()}
GROUPS = ("literal", "paraphrase", "paraphrase_with_literal_query")
FLAGGED_KINDS = frozenset({"reproduced", "missed"})  # a person raised these in the scenario
TIMEOUT_RULE = rr.entry_id(4)  # RG-9005: the timeout check of example-org/gamma
CONCURRENT_READERS = 3
WRITER_STEPS = 6
CONSISTENCY = (
    "every_reader_output_matches_one_generation",
    "second_writer_refused",
    "generation_unchanged_after_refused_writer",
    "writer_stopped_inside_transaction",
    "generation_unchanged_after_stopped_writer",
)

SYNTHETIC_NOTE = (
    "合成データです。ラベルは生成の規則から決まります。仕組みが動くことの検証で、実際の検索の質の"
    "検証ではありません。1 つの比率を成果としません。Claude Code の利用枠が減るとは主張しません。"
)

# What each metric may claim (the design's table, section 9.2). Every metric printed has one.
SCOPES = {
    "input_tokens": (
        "合成データでの見積もり（UTF-8 の 3 byte を 1 token）の分布。全量は件数に比例し、検索は予算で"
        "頭打ちになるので、差は規模で決まる。1 つの比率を成果にしない。実際の tokenizer の数と、"
        "セッション全体の消費（呼び出しの回数、追加のターン、prompt cache）は含まない"
    ),
    "retrieval": (
        "合成データでの値。ラベルは生成の規則から決まる。言い換えの query は語の一致の弱さを見るため"
        "のもので、人が書く query の分布は再現しない"
    ),
    "mandatory": (
        "止まってばかりで見逃し 0 でも意味が無いので、見逃した task の数と止まった task の割合を"
        "並べる。合成データでの値"
    ),
    "evidence": "合成データでの値。仕込んだ不一致（記録の後に変えた原文、消した原文）の検出の数",
    "repeated_findings_proxy": (
        "代理の指標。合成の評価用 PR の違反のある hunk について、その規則が task の検索結果に入って"
        "いたかだけを見る。実際の反復指摘率は、使い始めてからでないと測れない"
    ),
    "decisions": "仕組みの検証。合成のシナリオで数えた件数で、実際の判断待ちの件数は測っていない",
    "concurrency": (
        "この OS とこの SQLite での確認。ネットワークファイルシステムと別の host は含まない。"
        "より細かい検査は tests/unit/ の test_knowledge_store.py、test_knowledge_budget.py、"
        "test_knowledge_decisions.py"
    ),
    "estimate_vs_tokenizer": (
        "CI では測らない。手順は README の「評価」にあり、測った文字列の範囲のことしか言えない"
    ),
    "paraphrase": (
        "合成データでの値。言い換えは生成の作者が書いた 36 個で、人が書く query の分布は再現しない。"
        "fts5-paraphrase の query はレコードと検索の語を 1 つも共有しないように作ってあるので、"
        "任意のレコードを返さないのは作りのとおり。fts5-literal は元の語で検索した上限の目安。"
        "caller-expansion の値は渡した expansions のファイルについてだけ言える"
    ),
}


# Setting up -------------------------------------------------------------------------------


def _state(directory):
    """A state root under `directory` and the environment that names it."""
    env = {"AI_HARNESS_HOME": str(Path(directory) / "state")}
    return paths.state_root(env), env


def prepare(directory, size, seed=kc.SEED):
    """Index the corpus of `size` and write its source texts. Returns (root, scenario)."""
    root, _ = _state(directory)
    made = kc.scenario(size, seed)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(made.config))
    corpus = Path(directory) / "corpus.json"
    corpus.write_text(json.dumps(made.corpus, ensure_ascii=False), encoding="utf-8")
    ingest.update(root, build("file", {"input": corpus}), now=INDEXED)
    kc.write_sources(root, made)
    return root, made


def task_context(task, workspace=None):
    """The task-context a runner would hand over for a corpus task."""
    project = kc.PROJECTS[task["project_id"]]
    return {
        "schema_version": 1,
        "task_id": task["task_id"],
        "workspace_id": workspace or f"ws-{task['principal']}",
        "principal": task["principal"],
        "project_id": task["project_id"],
        "component_id": task["component_id"],
        "repo": project["repo"],
        "base_sha": None,
        "changed_paths": list(task["paths"]),
        "resource_types": list(task["resource_types"]),
    }


def _items_by_id(made):
    return {
        model.record_id(item["origin"]["kind"], item["origin"]["key"]): item
        for item in made.corpus["items"]
    }


# Full load ---------------------------------------------------------------------------------


def _readable(item, holder):
    access = item.get("access")
    return (
        access is not None
        and access["classification"] in holder["classifications"]
        and bool(set(access["readers"]) & set(holder["readers"]))
    )


def full_load_tokens(made, task, rejected):
    """Tokens of every approved record of the organization and the task's project that the
    task's principal reads, loaded whole (its sources' texts)."""
    holder = made.config["principals"][task["principal"]]
    total = 0
    for item in made.corpus["items"]:
        if item["origin"]["key"] in rejected or item["status"] != "approved":
            continue
        if item["scope"]["project_id"] not in (None, task["project_id"]):
            continue
        if not _readable(item, holder):
            continue
        for evidence in item["evidence"]:
            text = made.sources.get(evidence["locator"])
            total += terms.estimate_tokens(text) if text is not None else 0
    return total


# Search + fetch ----------------------------------------------------------------------------


def wants_fetch(shown, query):
    """The fixed rule for "read applies_when, fetch only when it applies to this work"."""
    words = set(terms.terms(query))
    return any(words & set(terms.terms(sentence)) for sentence in shown["applies_when"])


def _inner(text):
    """The text inside an untrusted envelope."""
    lines = text.split("\n")
    return "\n".join(lines[1:-1])


def _charge(root, task_id, principal, result, command, ids):
    return budget.charge(
        root,
        budget.Task(task_id),
        principal,
        command=command,
        ids=ids,
        tokens=result["budget"]["used_tokens"],
        generation=result["snapshot"]["generation"],
        now=SEARCHED,
    )


def searched(root, search_fn, principal, request, task_id=None):
    """One search, counted against `task_id` when given, as `knowledge context` does."""
    result = search_fn(root, principal, request, SEARCHED)
    if task_id is not None and result["status"] == "ok":
        returned = [shown["id"] for shown in result["records"] + result["proposed"]]
        if not _charge(root, task_id, principal, result, "search", returned).allowed:
            stopped = search.stopped("task_budget_exhausted", request, result["mandatory"])
            result = {**result, **stopped}
    return result


def run_task(root, search_fn, task):
    """The procedure for one task. Returns what it cost and what it returned."""
    found = task_context(task)
    principal = task["principal"]
    request = context.request(found, query=task["query"])
    first = searched(root, search_fn, principal, request, task["task_id"])
    final, stops = first, []
    if first["status"] != "ok":
        stops.append(first["status"])
    if first["status"] == "mandatory_overflow":
        raised = context.request(
            found,
            query=task["query"],
            max_tokens=first["mandatory"]["tokens"] + search.DEFAULT_MAX_TOKENS,
        )
        final = searched(root, search_fn, principal, raised, task["task_id"])
        if final["status"] != "ok":
            stops.append(final["status"])
    search_tokens = final["budget"]["used_tokens"] if final["status"] == "ok" else 0
    fetches = []
    for shown in final["records"] if final["status"] == "ok" else []:
        if not wants_fetch(shown, task["query"]):
            continue
        result = fetch.run(root, principal, fetch.Request(shown["id"], max_tokens=FETCH_TOKENS))
        if not _charge(root, task["task_id"], principal, result, "fetch", [shown["id"]]).allowed:
            stops.append("task_budget_exhausted")
            break
        fetches.append((shown, result))
    returned = [shown["id"] for shown in final["records"]]
    return {
        "first_status": first["status"],
        "stops": stops,
        "returned": returned,
        "mandatory_returned": set(task["mandatory"]) <= set(returned),
        "search_tokens": search_tokens,
        "fetch_tokens": sum(result["budget"]["used_tokens"] for _, result in fetches),
        "fetches": fetches,
    }


def relevant(task, items):
    topic = TOPIC_OF_WORDING[task["literal_query"]]
    on_topic = {i for i in task["applicable"] if topic in items[i]["facets"]["topic"]}
    return on_topic | set(task["mandatory"])


def _retrieval(pairs):
    """Pooled precision and recall over (returned ids, relevant ids) pairs."""
    hit = sum(len(set(got) & wanted) for got, wanted in pairs)
    got = sum(len(set(ids)) for ids, _ in pairs)
    wanted = sum(len(ids) for _, ids in pairs)
    return {
        "tasks": len(pairs),
        "precision": round(hit / got, 4) if got else None,
        "recall": round(hit / wanted, 4) if wanted else None,
        "returned": got,
        "relevant": wanted,
    }


def distribution(values):
    """mean, median, p95 (nearest rank), min and max of whole numbers."""
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return {
        "mean": round(statistics.fmean(ordered), 1),
        "median": statistics.median(ordered),
        "p95": ordered[rank - 1],
        "min": ordered[0],
        "max": ordered[-1],
    }


# Evidence ----------------------------------------------------------------------------------


def evidence_checks(root, made, runs):
    """Fetched fragments read back against their recorded hash, and the planted mismatches."""
    attempts, complete, verified, reasons = 0, 0, 0, {}
    for run in runs:
        for shown, result in run["fetches"]:
            attempts += 1
            for row in result["unfetched"]:
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
            if result["status"] != "ok" or result["next_offset"] is not None:
                continue
            if len(result["fragments"]) != 1 or len(shown["evidence"]) != 1:
                continue
            complete += 1
            (fragment,) = result["fragments"]
            text = _inner(fragment["text"])
            verified += records.sha256(text) == shown["evidence"][0]["content_sha256"]
    planted = {
        "stale_source": "hash_mismatch",
        "missing_source": "source_unavailable",
    }
    detected = {}
    for trap, reason in planted.items():
        (record_id,) = made.traps[trap]
        result = fetch.run(root, kc.PRINCIPAL_ALL, fetch.Request(record_id))
        detected[trap] = [row["reason"] for row in result["unfetched"]] == [reason]
    (injected,) = made.traps["instruction_text"]
    result = fetch.run(root, kc.PRINCIPAL_ALL, fetch.Request(injected))
    return {
        "fetches": attempts,
        "fetches_unfetched": reasons,
        "fragments_read_back": complete,
        "fragments_hash_match": verified,
        "hash_match_rate": round(verified / complete, 4) if complete else None,
        "planted_mismatches": len(planted),
        "planted_mismatches_detected": sum(detected.values()),
        "instruction_text_flagged": bool(result["fragments"])
        and all(fragment["instruction_like"] for fragment in result["fragments"]),
        "instruction_text_record_unchanged": result["record"]
        == {
            "status": "approved",
            "enforcement": "advisory",
            "kind": result["record"]["kind"],
            "content_hash": result["record"]["content_hash"],
        },
    }


# Decisions ---------------------------------------------------------------------------------


def decision_queue(root, made):
    """One request per task; every fifth task asks the same from a second workspace."""
    statuses = {"accepted": 0, "duplicate": 0}
    for number, task in enumerate(made.tasks, start=1):
        wording = task["literal_query"]
        ask = {
            "question": f"{task['task_id']} で{wording}の値を変えてよいか",
            "options": ["変える", "変えない"],
            "evidence_ids": [sorted(task["mandatory"])[0]],
            "risk": "low",
            "now": SEARCHED,
        }
        workspaces = [None, "ws-second"] if number % 5 == 0 else [None]
        for workspace in workspaces:
            answer = decisions.put(root, task_context(task, workspace), **ask)
            statuses[answer["status"]] += 1
    listing = decisions.listing(root, SEARCHED)
    return {
        "puts": sum(statuses.values()),
        "accepted": statuses["accepted"],
        "duplicates_dropped": statuses["duplicate"],
        "open": sum(1 for row in listing["requests"] if row["status"] == "open"),
    }


# Repeated findings (proxy) -----------------------------------------------------------------


def _hunk(kind):
    added = ("+" + gh.EVALUATION_KINDS[kind]).split("\n")
    header = f"@@ -20,2 +20,{2 + len(added)} @@ def download(url):"
    return "\n".join([header, "     session = make_session()", *added, "     return response"])


def repeated_findings(directory, search_fn):
    """The thirty evaluation hunks of the review scenario, each one task on example-org/gamma:
    for the hunks a person raised, was the repo's timeout rule in the task's search result?"""
    root, env = _state(directory)
    rr.write(root)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    ingest.update(root, build("review-registry", {"root": root}), now=INDEXED)
    rule = model.record_id("review-registry", TIMEOUT_RULE)
    flagged = in_result = checked = clean_findings = 0
    for number, kind in enumerate(gh.THIRTY_EVALUATION, start=1):
        path = f"src/{kind}_{number:02d}.py"
        task = {
            "schema_version": 1,
            "task_id": f"EVAL-{number:04d}",
            "workspace_id": "ws-eval",
            "principal": "workspace-a",
            "project_id": "project-gamma",
            "component_id": None,
            "repo": rr.GAMMA,
            "base_sha": None,
            "changed_paths": [path],
            "resource_types": [],
        }
        result = search_fn(root, task["principal"], context.request(task), SEARCHED)
        returned = {shown["id"] for shown in result["records"]}
        diff = Path(directory) / f"{task['task_id']}.diff"
        diff.write_text(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{_hunk(kind)}\n",
            encoding="utf-8",
        )
        found = check.run(root, task, check.read(str(diff)), env=env)
        hit = any(row["id"] == rule for row in found["violations"])
        if kind in FLAGGED_KINDS:
            flagged += 1
            in_result += rule in returned
            checked += hit
        else:
            clean_findings += hit
    return {
        "hunks": len(gh.THIRTY_EVALUATION),
        "flagged_hunks": flagged,
        "rule_in_search_result": in_result,
        "rate": round(in_result / flagged, 4) if flagged else None,
        "flagged_hunks_the_check_flags": checked,
        "check_findings_on_unflagged_hunks": clean_findings,
        "note": (
            "代理の指標です。実際の反復指摘率は使い始めてからでないと測れません。宣言的検査の"
            "requests.(get|post) は client.get の hunk を見ないので、check が拾うのは requests.post"
            " の hunk だけです。"
        ),
    }


# Concurrency -------------------------------------------------------------------------------


def _changed_corpus(made, step):
    corpus = json.loads(json.dumps(made.corpus))
    for item in corpus["items"][:5]:
        item["summary"] = f"{item['summary'][:200]}（版 {step}）"
    return corpus


def concurrency(directory, search_fn, size=kc.SIZES[0]):
    """Readers during a writer, two writers, and a writer stopped inside its transaction."""
    root, _ = prepare(directory, size)
    made = kc.scenario(size)
    task = made.tasks[1]
    request = context.request(task_context(task), query=task["query"])

    def answer():
        return records.canonical(search_fn(root, task["principal"], request, SEARCHED))

    expected = {
        search_fn(root, task["principal"], request, SEARCHED)["snapshot"]["generation"]: answer()
    }
    observed, errors, stop = [], [], threading.Event()

    def read():
        try:
            while not stop.is_set():
                text = answer()
                observed.append((json.loads(text)["snapshot"]["generation"], text))
        except BaseException as error:  # noqa: BLE001 - reported in the result
            errors.append(type(error).__name__)

    readers = [threading.Thread(target=read) for _ in range(CONCURRENT_READERS)]
    for thread in readers:
        thread.start()
    try:
        for step in range(1, WRITER_STEPS + 1):
            path = Path(directory) / f"step-{step}.json"
            path.write_text(json.dumps(_changed_corpus(made, step), ensure_ascii=False))
            summary = ingest.update(root, build("file", {"input": path}), now=INDEXED)
            expected[summary["generation"]["after"]] = answer()
    finally:
        stop.set()
        for thread in readers:
            thread.join(30)
    one_generation = bool(observed) and all(expected.get(g) == text for g, text in observed)

    with store.reader(root) as reader:
        before = reader.generation
    refused = []

    def second_writer():
        try:
            ingest.update(root, build("file", {"input": Path(directory) / "corpus.json"}))
            refused.append(False)
        except QualityGateError:
            refused.append(True)

    with store.writer(root):
        thread = threading.Thread(target=second_writer)
        thread.start()
        thread.join(30)
    with store.reader(root) as reader:
        after_second = reader.generation

    keys = [f"interrupted-{n}" for n in range(3)]
    batch_records = sorted(
        (
            model.normalize(
                {**made.corpus["items"][0], "origin": {"kind": "eval", "key": key}}, INDEXED
            )
            for key in keys
        ),
        key=lambda record: record["id"],
    )
    # Not JSON: the third record fails inside the transaction, after the first two were written.
    batch_records[-1]["approval"] = {"approver": {"not", "json"}}
    settings = config.load(root)
    batch = store.Batch(
        source="file:interrupted",
        adapter="file",
        cursor=None,
        complete=False,
        records=batch_records,
        config_hash=config.config_hash(settings),
    )
    stopped = False
    with store.writer(root) as writer:
        try:
            writer.apply(batch, INDEXED)
        except TypeError:
            stopped = True
    with store.reader(root) as reader:
        after_stop = reader.generation
        written = reader.records(
            config.principal(settings, kc.PRINCIPAL_ALL), ids=[r["id"] for r in batch_records]
        )
    return {
        "readers": CONCURRENT_READERS,
        "writer_steps": WRITER_STEPS,
        "reader_errors": len(errors),
        "every_reader_output_matches_one_generation": one_generation,
        "second_writer_refused": refused == [True],
        "generation_unchanged_after_refused_writer": after_second == before,
        "writer_stopped_inside_transaction": stopped,
        "generation_unchanged_after_stopped_writer": after_stop == after_second,
        "records_left_by_stopped_writer": len(written),
        "tests": [
            "tests/unit/test_knowledge_store.py::test_readers_never_see_a_mix_of_two_generations",
            "tests/unit/test_knowledge_store.py::test_a_second_writer_from_another_thread_is_refused_too",
            "tests/unit/test_knowledge_store.py::test_an_error_inside_the_transaction_changes_nothing",
        ],
    }


# One size ----------------------------------------------------------------------------------


def evaluate_size(directory, size, search_fn, seed=kc.SEED):
    root, made = prepare(directory, size, seed)
    items = _items_by_id(made)
    rejected = {key for keys in made.traps["rejected"].values() for key in keys}
    runs, full = [], []
    for task in made.tasks:
        runs.append(run_task(root, search_fn, task))
        full.append(full_load_tokens(made, task, rejected))
    pairs = {group: [] for group in GROUPS}
    for task, run in zip(made.tasks, runs):
        wanted = relevant(task, items)
        pairs["paraphrase" if task["paraphrase"] else "literal"].append((run["returned"], wanted))
        if task["paraphrase"]:
            literal = context.request(task_context(task), query=task["literal_query"])
            result = search_fn(root, task["principal"], literal, SEARCHED)
            pairs["paraphrase_with_literal_query"].append(
                ([shown["id"] for shown in result["records"]], wanted)
            )
    applicable = [(run["returned"], set(task["applicable"])) for task, run in zip(made.tasks, runs)]
    pooled_applicable = _retrieval(applicable)
    first_stops = [run["first_status"] for run in runs if run["first_status"] != "ok"]
    all_stops = [status for run in runs for status in run["stops"]]
    return {
        "records": len(made.corpus["items"]),
        "tasks": len(made.tasks),
        "input_tokens": {
            "full_load": distribution(full),
            "search_and_fetch": distribution(
                [r["search_tokens"] + r["fetch_tokens"] for r in runs]
            ),
            "search_only": distribution([r["search_tokens"] for r in runs]),
            "fetch_only": distribution([r["fetch_tokens"] for r in runs]),
            "fetched_records": sum(len(r["fetches"]) for r in runs),
        },
        "retrieval": {
            **{group: _retrieval(pairs[group]) for group in GROUPS},
            "applicable_precision": pooled_applicable["precision"],
            "applicable_recall": pooled_applicable["recall"],
        },
        "mandatory": {
            "missed_with_status_ok": sum(
                1 for run in runs if run["first_status"] == "ok" and not run["mandatory_returned"]
            ),
            "stopped_tasks": len(first_stops),
            "stopped_rate": round(len(first_stops) / len(runs), 4),
            "stops": {status: all_stops.count(status) for status in sorted(set(all_stops))},
        },
        "evidence": evidence_checks(root, made, runs),
        "decisions": decision_queue(root, made),
    }


def evaluate(sizes=kc.SIZES, method="fts5", seed=kc.SEED, work_dir=None):
    """Every metric for `sizes` with the search `method` (METHODS). Deterministic."""
    search_fn = METHODS[method]
    base = Path(tempfile.mkdtemp(prefix="sre-harness-eval.", dir=work_dir))
    try:
        result = {
            "schema_version": 1,
            "synthetic": True,
            "note": SYNTHETIC_NOTE,
            "method": method,
            "seed": seed,
            "budgets": {
                "search_max_tokens": search.DEFAULT_MAX_TOKENS,
                "fetch_max_tokens": FETCH_TOKENS,
                "task_total": budget.DEFAULT_LIMIT,
            },
            "estimator": terms.ESTIMATOR,
            "sizes": {},
        }
        for size in sizes:
            folder = base / f"size-{size}"
            folder.mkdir()
            result["sizes"][str(size)] = evaluate_size(folder, size, search_fn, seed)
        (base / "review").mkdir()
        result["repeated_findings_proxy"] = repeated_findings(base / "review", search_fn)
        (base / "concurrency").mkdir()
        result["concurrency"] = concurrency(base / "concurrency", search_fn)
        result["estimate_vs_tokenizer"] = {
            "measured": False,
            "note": "README の「評価」の手順で手動で測る",
        }
        result["claim_scope"] = dict(SCOPES)
        return result
    finally:
        shutil.rmtree(base, ignore_errors=True)


# Paraphrase cases --------------------------------------------------------------------------

PARAPHRASE_BUDGETS = (search.DEFAULT_MAX_TOKENS, 20000)
EXPANSION_FILE_BYTES = 1024 * 1024
# How a case is queried: (case, its expansion words or None) -> (query, expansion words).
PARAPHRASE_PLANS = {
    "fts5-paraphrase": lambda case, words: (case["query"], None),
    "fts5-literal": lambda case, words: (case["literal_query"], None),
    "caller-expansion": lambda case, words: (case["query"], words or []),
}
PARAPHRASE_DEFINITIONS = {
    "relevant": (
        "case の topic の、case に適用される承認済みの任意のレコード（scope、facet、principal が"
        "読める、置換されていない）。必須のレコードは query に関係なく返るので、正解からも返った"
        "集合からも除く"
    ),
    "returned": "search が返した任意のレコード。止まった search は 0 件（stopped に数える）",
    "recall": "区分の case をまとめて、返した正解の数 / 正解の数",
    "precision": "区分の case をまとめて、返した正解の数 / 返した数（返した数が 0 なら null）",
    "stdev": "expansions のファイルが 2 つ以上のときの標本標準偏差（1 つなら null）",
}


def prepare_paraphrase(directory, size, seed=kc.SEED):
    """Index the corpus of `size` with the paraphrase records. Returns (root, scenario)."""
    root, _ = _state(directory)
    made = kc.paraphrase_scenario(size, seed)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(made.config))
    corpus = Path(directory) / "corpus.json"
    corpus.write_text(json.dumps(made.corpus, ensure_ascii=False), encoding="utf-8")
    ingest.update(root, build("file", {"input": corpus}), now=INDEXED)
    return root, made


def case_search(root, search_fn, case, *, query, expand=None, max_tokens):
    """One search for a case, as `knowledge context` makes it from the case's task."""
    request = dataclasses.replace(
        kc.case_request(case), query=query, expand=expand, max_tokens=max_tokens
    )
    return search_fn(root, case["principal"], request, SEARCHED)


def advisory(result):
    """The advisory records a search returned; none when it stopped."""
    if result["status"] != "ok":
        return set()
    return {view["id"] for view in result["records"] if "mandatory" not in view["why_selected"]}


def pooled(rows):
    """Recall and precision pooled over (returned ids, relevant ids, stopped, refused) rows."""
    hits = sum(len(got & wanted) for got, wanted, _, _ in rows)
    returned = sum(len(got) for got, _, _, _ in rows)
    relevant = sum(len(wanted) for _, wanted, _, _ in rows)
    return {
        "cases": len(rows),
        "relevant": relevant,
        "returned": returned,
        "hits": hits,
        "recall": round(hits / relevant, 4) if relevant else None,
        "precision": round(hits / returned, 4) if returned else None,
        "stopped": sum(1 for _, _, stopped, _ in rows if stopped),
        "refused": sum(1 for _, _, _, refused in rows if refused),
    }


def paraphrase_retrieval(root, cases, search_fn, plan, max_tokens, words=None):
    """{category: pooled metrics} of `plan` (PARAPHRASE_PLANS) with the search `search_fn`.
    `words` is {case id: [word, ...]}, the expansion words; an expansion `knowledge search`
    would refuse (exit 2) returns nothing and is counted in `refused`."""
    rows = {category: [] for category in kc.PARAPHRASE_CATEGORIES}
    for case in cases:
        query, expand = PARAPHRASE_PLANS[plan](case, (words or {}).get(case["id"]))
        refused = False
        if expand is not None:
            try:
                expand = search.expansion(expand)
            except InputError:
                refused = True
        got, stopped = set(), False
        if not refused:
            result = case_search(
                root, search_fn, case, query=query, expand=expand, max_tokens=max_tokens
            )
            got, stopped = advisory(result), result["status"] != "ok"
        rows[case["category"]].append((got, set(case["relevant"]), stopped, refused))
    return {category: pooled(found) for category, found in rows.items()}


def spread(values):
    """mean, min, max and sample standard deviation (None for one value) of the given values;
    the values that are None are left out."""
    known = [value for value in values if value is not None]
    if not known:
        return {"mean": None, "min": None, "max": None, "stdev": None}
    return {
        "mean": round(statistics.fmean(known), 4),
        "min": min(known),
        "max": max(known),
        "stdev": round(statistics.stdev(known), 4) if len(known) > 1 else None,
    }


def load_expansions(path, case_ids):
    """An expansions file: {"model": str, "run": int, "terms": {case id: [str, ...]}}. A case it
    does not name is searched with no expansion word. ValueError when the file is not one."""
    data = Path(path).read_bytes()
    if len(data) > EXPANSION_FILE_BYTES:
        raise ValueError(f"{path} is larger than {EXPANSION_FILE_BYTES} bytes")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{path} is not UTF-8 JSON") from None
    shaped = (
        isinstance(value, dict)
        and set(value) == {"model", "run", "terms"}
        and isinstance(value["model"], str)
        and value["model"].strip()
        and isinstance(value["run"], int)
        and not isinstance(value["run"], bool)
        and isinstance(value["terms"], dict)
        and all(
            isinstance(words, list) and all(isinstance(word, str) for word in words)
            for words in value["terms"].values()
        )
    )
    if not shaped:
        raise ValueError(f'{path} is not {{"model": str, "run": int, "terms": {{id: [str]}}}}')
    unknown = sorted(set(value["terms"]) - set(case_ids))
    if unknown:
        raise ValueError(f"{path} names cases that do not exist: {', '.join(unknown[:5])}")
    return value


def expansion_input(seed=kc.SEED):
    """What a caller that widens the queries is given: case ids and paraphrased queries only."""
    cases = kc.paraphrase_scenario(kc.SIZES[0], seed).paraphrase_cases
    return [{"id": case["id"], "query": case["query"]} for case in cases]


def _paraphrase_size(directory, size, search_fn, budgets, expansions, seed):
    root, made = prepare_paraphrase(directory, size, seed)
    cases = made.paraphrase_cases
    by_budget = {}
    for max_tokens in budgets:
        found = {
            plan: paraphrase_retrieval(root, cases, search_fn, plan, max_tokens)
            for plan in ("fts5-paraphrase", "fts5-literal")
        }
        if expansions:
            files = [
                {
                    "model": value["model"],
                    "run": value["run"],
                    "categories": paraphrase_retrieval(
                        root, cases, search_fn, "caller-expansion", max_tokens, value["terms"]
                    ),
                }
                for value in expansions
            ]
            found["caller-expansion"] = {
                "files": files,
                "summary": {
                    category: {
                        name: spread([f["categories"][category][name] for f in files])
                        for name in ("recall", "precision")
                    }
                    for category in kc.PARAPHRASE_CATEGORIES
                },
            }
        by_budget[str(max_tokens)] = found
    return {"records": len(made.corpus["items"]), "budgets": by_budget}


def evaluate_paraphrase(
    sizes=kc.SIZES,
    budgets=PARAPHRASE_BUDGETS,
    expansions=(),
    method="fts5",
    seed=kc.SEED,
    work_dir=None,
):
    """Recall and precision of the paraphrase cases by category, size, budget and plan, with the
    search `method` (METHODS). `expansions` are loaded expansions files. Deterministic."""
    search_fn = METHODS[method]
    cases = kc.paraphrase_scenario(kc.SIZES[0], seed).paraphrase_cases
    base = Path(tempfile.mkdtemp(prefix="sre-harness-paraphrase.", dir=work_dir))
    try:
        result = {
            "schema_version": 1,
            "synthetic": True,
            "note": SYNTHETIC_NOTE,
            "method": method,
            "seed": seed,
            "estimator": terms.ESTIMATOR,
            "definitions": dict(PARAPHRASE_DEFINITIONS),
            "categories": {
                category: sum(case["category"] == category for case in cases)
                for category in kc.PARAPHRASE_CATEGORIES
            },
            "budgets": list(budgets),
            "expansions": [{"model": v["model"], "run": v["run"]} for v in expansions],
            "sizes": {},
        }
        for size in sizes:
            folder = base / f"size-{size}"
            folder.mkdir()
            result["sizes"][str(size)] = _paraphrase_size(
                folder, size, search_fn, budgets, expansions, seed
            )
        result["claim_scope"] = {"paraphrase": SCOPES["paraphrase"]}
        return result
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _cell(value):
    return f"{_ratio(value['recall'])} / {_ratio(value['precision'])} / {value['returned']}"


def paraphrase_markdown(result):
    """One row per size, budget and category: relevant, then recall / precision / returned of
    each plan (and the spread over the expansions files when there are any)."""
    expanded = bool(result["expansions"])
    columns = ["規模", "予算", "区分", "case", "正解", "fts5-paraphrase", "fts5-literal"]
    if expanded:
        columns.append(
            f"caller-expansion（{len(result['expansions'])} ファイルの平均 / 最小 / 最大）"
        )
    text = (
        f"{SYNTHETIC_NOTE}\n\n言い換えの case（ja {result['categories']['ja']}、"
        f"en {result['categories']['en']}、cross {result['categories']['cross']}）、"
        f"検索方式 `{result['method']}`、seed {result['seed']}。各列は recall / precision / "
        "返した任意のレコードの数。必須のレコードは正解からも返した集合からも除いています。\n\n"
    )
    text += "| " + " | ".join(columns) + " |\n" + "|---" * len(columns) + "|\n"
    for size, value in result["sizes"].items():
        for max_tokens, found in value["budgets"].items():
            for category in kc.PARAPHRASE_CATEGORIES:
                literal = found["fts5-literal"][category]
                cells = [
                    f"{size} 件（索引 {value['records']} 件）",
                    max_tokens,
                    category,
                    str(literal["cases"]),
                    str(literal["relevant"]),
                    _cell(found["fts5-paraphrase"][category]),
                    _cell(literal),
                ]
                if expanded:
                    recall = found["caller-expansion"]["summary"][category]["recall"]
                    cells.append(
                        " / ".join(_ratio(recall[name]) for name in ("mean", "min", "max"))
                    )
                text += "| " + " | ".join(cells) + " |\n"
    text += "\n主張できる範囲:\n\n" + f"- 言い換え: {SCOPES['paraphrase']}\n"
    text += "".join(f"- {name}: {meaning}\n" for name, meaning in PARAPHRASE_DEFINITIONS.items())
    return text


# Output ------------------------------------------------------------------------------------


def _stats(value):
    return f"{value['mean']} / {value['median']} / {value['p95']} / {value['min']} / {value['max']}"


def _ratio(value):
    return "-" if value is None else f"{value:.4f}"


SCOPE_LABELS = {
    "input_tokens": "入力 token",
    "retrieval": "取得",
    "mandatory": "必須",
    "evidence": "根拠",
    "decisions": "判断待ち",
    "repeated_findings_proxy": "反復指摘の代理",
    "concurrency": "並行",
    "estimate_vs_tokenizer": "見積もりの差",
}


def _table(columns, rows):
    """A Markdown table whose last column names each row's scope, then the scopes it used."""
    text = "| " + " | ".join([*columns, "範囲"]) + " |\n" + "|---" * (len(columns) + 1) + "|\n"
    used = []
    for cells, scope in rows:
        text += "| " + " | ".join([*cells, SCOPE_LABELS[scope]]) + " |\n"
        if scope not in used:
            used.append(scope)
    text += "\n主張できる範囲:\n\n"
    text += "".join(f"- {SCOPE_LABELS[scope]}: {SCOPES[scope]}\n" for scope in used)
    return text + "\n"


def markdown(result):
    """The tables README's "評価" carries; every row names the scope it may claim."""
    sizes = list(result["sizes"])
    columns = ["指標", *(f"{size} 件" for size in sizes)]

    def row(label, scope, pick):
        return [label, *(pick(result["sizes"][size]) for size in sizes)], scope

    first = result["sizes"][sizes[0]]
    text = (
        f"{SYNTHETIC_NOTE}\n\n検索方式 `{result['method']}`、seed {result['seed']}、"
        f"task {first['tasks']} 件、principal 2 つ。\n\n"
    )
    text += "入力 token（平均 / 中央値 / p95 / 最小 / 最大）:\n\n"
    text += _table(
        columns,
        [
            row("全量ロード", "input_tokens", lambda s: _stats(s["input_tokens"]["full_load"])),
            row(
                "search + fetch",
                "input_tokens",
                lambda s: _stats(s["input_tokens"]["search_and_fetch"]),
            ),
            row("うち search", "input_tokens", lambda s: _stats(s["input_tokens"]["search_only"])),
            row("うち fetch", "input_tokens", lambda s: _stats(s["input_tokens"]["fetch_only"])),
        ],
    )
    labels = {
        "literal": "言い換えなし",
        "paraphrase": "言い換え",
        "paraphrase_with_literal_query": "言い換えの task を元の語で",
    }

    def pair(group):
        return lambda s: (
            f"{_ratio(s['retrieval'][group]['precision'])} / "
            f"{_ratio(s['retrieval'][group]['recall'])}"
        )

    rows = [
        row(f"{label}（{first['retrieval'][group]['tasks']} task）", "retrieval", pair(group))
        for group, label in labels.items()
    ]
    rows.append(
        row(
            "適用されるレコード全体に対して",
            "retrieval",
            lambda s: (
                f"{_ratio(s['retrieval']['applicable_precision'])} / "
                f"{_ratio(s['retrieval']['applicable_recall'])}"
            ),
        )
    )
    text += "取得の precision / recall（task をまとめて数えた値）:\n\n" + _table(columns, rows)
    text += "必須ルール、根拠、判断待ち:\n\n"
    text += _table(
        columns,
        [
            row(
                "必須の見逃し（最初の search が ok の task）",
                "mandatory",
                lambda s: str(s["mandatory"]["missed_with_status_ok"]),
            ),
            row(
                "最初の search が止まった task（割合）",
                "mandatory",
                lambda s: (
                    f"{s['mandatory']['stopped_tasks']}（{s['mandatory']['stopped_rate']:.2f}）"
                ),
            ),
            row(
                "止まった回数（理由ごと）",
                "mandatory",
                lambda s: "、".join(f"{k} {v}" for k, v in s["mandatory"]["stops"].items()),
            ),
            row(
                "fetch した断片の hash が記録と合う",
                "evidence",
                lambda s: (
                    f"{s['evidence']['fragments_hash_match']} / "
                    f"{s['evidence']['fragments_read_back']}"
                ),
            ),
            row(
                "仕込んだ不一致の検出",
                "evidence",
                lambda s: (
                    f"{s['evidence']['planted_mismatches_detected']} / "
                    f"{s['evidence']['planted_mismatches']}"
                ),
            ),
            row(
                "判断待ち（open）/ 重複で落とした",
                "decisions",
                lambda s: f"{s['decisions']['open']} / {s['decisions']['duplicates_dropped']}",
            ),
        ],
    )
    proxy, flow = result["repeated_findings_proxy"], result["concurrency"]
    holds = all(flow[key] for key in CONSISTENCY) and not flow["reader_errors"]
    holds = holds and not flow["records_left_by_stopped_writer"]
    text += "規模によらない指標:\n\n"
    text += _table(
        ["指標", "値"],
        [
            (
                [
                    "反復指摘率の代理（違反のある hunk のうち、規則が検索結果に入っていた）",
                    f"{proxy['rule_in_search_result']} / {proxy['flagged_hunks']}",
                ],
                "repeated_findings_proxy",
            ),
            (
                [
                    f"並行時の整合性（reader {flow['readers']} つと writer、writer 2 つ、"
                    "途中で止めた writer）",
                    "すべて成り立つ" if holds else "成り立たないものがある",
                ],
                "concurrency",
            ),
            (["見積もりと実 token の差", "測っていない"], "estimate_vs_tokenizer"),
        ],
    )
    return text


def evaluator_output(result):
    """The autonomous-improvement evaluator output (evaluation.schema.json#/$defs/output)."""
    metrics = {}
    for size, value in result["sizes"].items():
        for group in GROUPS:
            for name in ("precision", "recall"):
                number = value["retrieval"][group][name]
                if number is not None:
                    metrics[f"{group}_{name}_{size}"] = {
                        "value": number,
                        "rationale": SCOPES["retrieval"],
                    }
        metrics[f"mandatory_missed_{size}"] = {
            "value": float(value["mandatory"]["missed_with_status_ok"]),
            "rationale": SCOPES["mandatory"],
        }
        metrics[f"stopped_rate_{size}"] = {
            "value": value["mandatory"]["stopped_rate"],
            "rationale": SCOPES["mandatory"],
        }
        for side in ("full_load", "search_and_fetch"):
            metrics[f"input_tokens_{side}_median_{size}"] = {
                "value": float(value["input_tokens"][side]["median"]),
                "rationale": SCOPES["input_tokens"],
            }
    return {
        "metrics": metrics,
        "evidence": [
            {
                "id": f"size-{size}",
                "ref": f"knowledge_eval.json#/sizes/{size}",
                "note": SYNTHETIC_NOTE,
            }
            for size in result["sizes"]
        ],
        "notes": SYNTHETIC_NOTE,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--size", type=int, action="append", choices=kc.SIZES, help="repeat; default 50"
    )
    parser.add_argument("--method", choices=sorted(METHODS), default="fts5")
    parser.add_argument("--seed", type=int, default=kc.SEED)
    parser.add_argument(
        "--format", choices=("json", "markdown", "evaluator-output"), default="json"
    )
    parser.add_argument(
        "--work-dir", help="where the temporary state root goes (outside any repository)"
    )
    parser.add_argument(
        "--paraphrase", action="store_true", help="measure the paraphrase cases instead"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        action="append",
        help="with --paraphrase, a search budget; repeat (default 1500 and 20000)",
    )
    parser.add_argument(
        "--expansions",
        action="append",
        metavar="FILE",
        help='with --paraphrase, {"model", "run", "terms": {case id: [word]}}; repeat',
    )
    parser.add_argument(
        "--write-expansion-input",
        metavar="PATH",
        help="write the case ids and paraphrased queries a query widener is given, and stop",
    )
    args = parser.parse_args(argv)
    if args.write_expansion_input:
        cases = expansion_input(args.seed)
        Path(args.write_expansion_input).write_text(
            json.dumps(cases, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(json.dumps({"written": args.write_expansion_input, "cases": len(cases)}))
        return
    if not args.paraphrase:
        if args.max_tokens or args.expansions:
            parser.error("--max-tokens and --expansions need --paraphrase")
    else:
        if args.format == "evaluator-output":
            parser.error("--paraphrase prints json or markdown")
        if any(limit < 1 for limit in args.max_tokens or ()):
            parser.error("--max-tokens needs a whole number of at least 1")
        ids = [case["id"] for case in expansion_input(args.seed)]
        try:
            loaded = [load_expansions(path, ids) for path in args.expansions or ()]
        except (OSError, ValueError) as error:
            parser.error(str(error))
        found = evaluate_paraphrase(
            tuple(args.size or kc.SIZES),
            tuple(args.max_tokens or PARAPHRASE_BUDGETS),
            loaded,
            args.method,
            args.seed,
            args.work_dir,
        )
        if args.format == "markdown":
            print(paraphrase_markdown(found), end="")
        else:
            print(json.dumps(found, indent=2, ensure_ascii=False))
        return
    result = evaluate(tuple(args.size or (kc.SIZES[0],)), args.method, args.seed, args.work_dir)
    if args.format == "markdown":
        print(markdown(result), end="")
    elif args.format == "evaluator-output":
        print(json.dumps(evaluator_output(result), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
