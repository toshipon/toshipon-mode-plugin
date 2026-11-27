"""Probe a hybrid of FTS5 and vectors on the paraphrase cases. An experiment, not the harness.

Nothing here is part of `sre_harness`: the package gains no vector search, no model and no new
dependency. The script measures whether ranking the advisory records with vectors as well finds
what the paraphrased queries miss, with the same cases, the same labels and the same metrics as
`knowledge_eval --paraphrase` (it imports them). CI does not run it.

Optional requirements, none of which `requirements.txt` holds:

    - a Python whose sqlite3 can load extensions (`knowledge status` says `extension_loading`)
    - the `sqlite-vec` package: vectors are stored and searched in its `vec0` table, in an
      in-memory SQLite database (the harness index is never written)
    - the `sentence-transformers` package and a small multilingual embedding model on local
      disk (`--model PATH`, or a name already in the local model cache). The model is read with
      the offline settings of the model libraries (HF_HUB_OFFLINE, TRANSFORMERS_OFFLINE), so
      the run sends nothing anywhere; get the model before, by hand

Without them it prints what is missing and exits 2.

How a case is searched (`hybrid_search`, the same signature as METHODS of knowledge_eval):

    1. `search.run` with the query and a budget no record can exceed: its status, its mandatory
       records (which never depend on the query) and its order of the advisory records (FTS5)
    2. the candidates for vectors: the records the principal reads (`store.reader().records`),
       narrowed by `search.narrow` with the request (scope, facets, replaced records), approved
       and advisory. Vectors never add a record that `knowledge search` could not return
    3. the vector order: the candidates by cosine distance to the query, nearest first, keeping
       the first --vector-k of them and, with --max-distance, only those within it
    4. Reciprocal Rank Fusion of the two orders: score(r) = sum over the orders that hold r of
       1 / (K + rank(r)), rank from 1, K = --rrf-k (60 by default); ties by record id
    5. the budget as `knowledge search` fills it: the mandatory records first, then the fused
       order, the views and their tokens by `search.view` and `search.tokens`, stopping at the
       first record that does not fit

The text embedded for a record is its summary, applies_when, exceptions and facet words (topic
and resource type); for a query, the query. `--query-prefix` and `--passage-prefix` add what
some models expect (e5 models: "query: " and "passage: ").

Embedding is not assumed to be deterministic: `--runs N` embeds and measures N times and prints
the mean, min, max and standard deviation of every recall and precision.

Run it from `runtime/`, with the Python that has the optional packages:

    python -m examples.synthetic.knowledge_vector_probe --model /path/to/model \\
        --size 50 --size 300 --size 2000 --runs 3 --out probe.json
"""

import argparse
import dataclasses
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from examples.synthetic import knowledge_corpus as kc
from examples.synthetic import knowledge_eval as ke
from sre_harness.knowledge import config, search, store

UNBOUNDED = 10**9  # a budget no record exceeds: the whole FTS5 order
RRF_K = 60
VECTOR_KS = (5, 10, 20)
OPTIONAL = {"sqlite_vec": "sqlite-vec", "sentence_transformers": "sentence-transformers"}
OFFLINE = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
NOTE = (
    "合成データでの実験です。採否を決めるための測定で、harness の package には何も足していません。"
    "埋め込みの値は model と library の版で変わります。"
)


def missing(model):
    """What this environment lacks for the probe, in words. Empty when it can run."""
    found = []
    if not store.extension_loading():
        found.append(
            f"this Python's sqlite3 cannot load extensions ({sys.executable}, "
            f"Python {platform.python_version()}, SQLite {store.sqlite_version()})"
        )
    for module, package in OPTIONAL.items():
        if importlib.util.find_spec(module) is None:
            found.append(f"the {package} package is not installed")
    if model is None:
        found.append("--model: a local embedding model (a directory, or a name in the cache)")
    return found


def model_card(model):
    """Name, version, size and license of the model, as far as the files say."""
    path = Path(model)
    card = {"name": path.name if path.exists() else model, "version": None, "bytes": None}
    card["license"] = None
    if not path.is_dir():
        return card
    if path.parent.name == "snapshots":
        card["version"] = path.name
        card["name"] = path.parent.parent.name.removeprefix("models--").replace("--", "/")
    card["bytes"] = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    readme = path / "README.md"
    if readme.is_file():
        lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if line.startswith("license:"):
                    card["license"] = line.split(":", 1)[1].strip()
    return card


class Embedder:
    """The model, read offline. `encode` returns unit vectors as lists of floats."""

    def __init__(self, model, query_prefix="", passage_prefix=""):
        os.environ.update(OFFLINE)
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model, device="cpu", local_files_only=True)
        self.query_prefix, self.passage_prefix = query_prefix, passage_prefix

    def encode(self, texts, prefix):
        vectors = self.model.encode(
            [prefix + text for text in texts], normalize_embeddings=True, convert_to_numpy=True
        )
        return [[float(x) for x in vector] for vector in vectors]

    @property
    def dimension(self):
        return int(self.model.get_sentence_embedding_dimension() or 0)


def record_text(record):
    facets = record["facets"]
    parts = [record["summary"], *record["applies_when"], *record["exceptions"]]
    return "\n".join([*parts, *facets["topic"], *facets["resource_type"]])


class Vectors:
    """The records of one index in a sqlite-vec `vec0` table, in memory."""

    def __init__(self, rows, embedder):
        import sqlite_vec

        self.embedder = embedder
        self.connection = sqlite3.connect(":memory:")
        self.connection.enable_load_extension(True)
        sqlite_vec.load(self.connection)
        self.connection.enable_load_extension(False)
        self.ids = [row["id"] for row in rows]
        dimension = embedder.dimension
        self.connection.execute(
            f"CREATE VIRTUAL TABLE vectors USING vec0(embedding float[{dimension}] "
            "distance_metric=cosine)"
        )
        vectors = embedder.encode([record_text(row) for row in rows], embedder.passage_prefix)
        self.connection.executemany(
            "INSERT INTO vectors (rowid, embedding) VALUES (?, ?)",
            [(n, sqlite_vec.serialize_float32(v)) for n, v in enumerate(vectors, start=1)],
        )
        self._queries = {}

    def nearest(self, query):
        """[(id, cosine distance)] of every record, nearest first."""
        import sqlite_vec

        if query not in self._queries:
            (vector,) = self.embedder.encode([query], self.embedder.query_prefix)
            rows = self.connection.execute(
                "SELECT rowid, distance FROM vectors WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                (sqlite_vec.serialize_float32(vector), len(self.ids)),
            )
            self._queries[query] = [(self.ids[rowid - 1], distance) for rowid, distance in rows]
        return self._queries[query]


def fuse(orders, rrf_k=RRF_K):
    """Reciprocal Rank Fusion: ids by the sum of 1 / (rrf_k + rank), rank from 1."""
    scores = {}
    for order in orders:
        for rank, record_id in enumerate(order, start=1):
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores, key=lambda record_id: (-scores[record_id], record_id))


def hybrid(vectors, vector_k, max_distance=None, rrf_k=RRF_K):
    """A search function (root, principal, request, now) -> result, as knowledge_eval's."""

    def run(root, name, request, now):
        wide = search.run(root, name, dataclasses.replace(request, max_tokens=UNBOUNDED), now)
        if wide["status"] != "ok":
            return search.run(root, name, request, now)
        must = [v for v in wide["records"] if "mandatory" in v["why_selected"]]
        lexical = [v["id"] for v in wide["records"] if "mandatory" not in v["why_selected"]]
        settings = config.load(root)
        principal = config.principal(settings, name)
        with store.reader(root) as reader:
            rows = reader.records(principal)
            provenance = reader.provenance(principal)
            sources = {row["name"]: row for row in reader.sources()}
        live = search.narrow(rows, request).live
        allowed = {row["id"]: row for row in live if row["enforcement"] != "mandatory"}
        near = [
            record_id
            for record_id, distance in vectors.nearest(request.query)
            if record_id in allowed and (max_distance is None or distance <= max_distance)
        ][:vector_k]
        views = {v["id"]: v for v in wide["records"]}
        left = request.max_tokens - sum(search.tokens(v) for v in must)
        chosen, truncated = [], []
        for record_id in fuse([lexical, near], rrf_k):
            row = allowed[record_id]
            shown = views.get(record_id) or search.view(
                row,
                search.reasons(row, request),
                search.freshness(
                    row, provenance.get(record_id), sources, now, request.stale_after_hours
                ),
            )
            cost = search.tokens(shown)
            if not truncated and cost <= left:
                chosen.append(shown)
                left -= cost
            else:
                truncated.append((record_id, cost))
        return {
            **wide,
            "records": must + chosen,
            "budget": {
                **wide["budget"],
                "max_tokens": request.max_tokens,
                "used_tokens": request.max_tokens - left,
            },
            "truncated": {
                "count": len(truncated),
                "ids": [record_id for record_id, _ in truncated],
                "tokens_needed": sum(cost for _, cost in truncated),
            },
        }

    return run


def _readable_rows(root):
    """Every record of the index once, whatever principal reads it (for embedding only)."""
    settings = config.load(root)
    seen = {}
    with store.reader(root) as reader:
        for name in sorted(settings["principals"]):
            for row in reader.records(config.principal(settings, name)):
                seen[row["id"]] = row
    return [seen[key] for key in sorted(seen)]


def probe_size(directory, size, embedder, budgets, vector_ks, max_distance, rrf_k):
    root, made = ke.prepare_paraphrase(directory, size)
    vectors = Vectors(_readable_rows(root), embedder)
    found = {}
    for max_tokens in budgets:
        by_k = {}
        for vector_k in vector_ks:
            search_fn = hybrid(vectors, vector_k, max_distance, rrf_k)
            by_k[str(vector_k)] = ke.paraphrase_retrieval(
                root, made.paraphrase_cases, search_fn, "fts5-paraphrase", max_tokens
            )
        found[str(max_tokens)] = by_k
    return {"records": len(made.corpus["items"]), "budgets": found}


def summarize(runs):
    """The spread of every recall and precision over the runs."""
    first = runs[0]
    out = {}
    for size, value in first.items():
        out[size] = {"records": value["records"], "budgets": {}}
        for budget, by_k in value["budgets"].items():
            out[size]["budgets"][budget] = {
                k: {
                    category: {
                        name: ke.spread(
                            [run[size]["budgets"][budget][k][category][name] for run in runs]
                        )
                        for name in ("recall", "precision")
                    }
                    for category in kc.PARAPHRASE_CATEGORIES
                }
                for k in by_k
            }
    return out


def markdown(summary):
    text = f"{NOTE}\n\n| 規模 | 予算 | vector-k | 区分 | recall（平均 / 最小 / 最大 / 標準偏差） |"
    text += " precision（平均） |\n|---|---|---|---|---|---|\n"
    for size, value in summary.items():
        for budget, by_k in value["budgets"].items():
            for k, categories in by_k.items():
                for category, metrics in categories.items():
                    recall = metrics["recall"]
                    cells = " / ".join(
                        ke._ratio(recall[name]) for name in ("mean", "min", "max", "stdev")
                    )
                    text += (
                        f"| {size} 件（索引 {value['records']} 件） | {budget} | {k} | {category} "
                        f"| {cells} | {ke._ratio(metrics['precision']['mean'])} |\n"
                    )
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--model", help="a local embedding model: a directory or a cached name")
    parser.add_argument("--size", type=int, action="append", choices=kc.SIZES)
    parser.add_argument("--max-tokens", type=int, action="append", help="repeat")
    parser.add_argument("--vector-k", type=int, action="append", help="repeat (5, 10, 20)")
    parser.add_argument("--max-distance", type=float, help="cosine distance a vector hit needs")
    parser.add_argument("--rrf-k", type=int, default=RRF_K)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--query-prefix", default="")
    parser.add_argument("--passage-prefix", default="")
    parser.add_argument("--out", help="also write the JSON here")
    parser.add_argument("--work-dir", help="where the temporary state roots go")
    args = parser.parse_args(argv)
    lacking = missing(args.model)
    if lacking:
        print(json.dumps({"status": "missing", "missing": lacking}, ensure_ascii=False))
        print("knowledge_vector_probe cannot run: " + "; ".join(lacking), file=sys.stderr)
        return 2
    sizes = tuple(args.size or kc.SIZES)
    budgets = tuple(args.max_tokens or ke.PARAPHRASE_BUDGETS)
    vector_ks = tuple(args.vector_k or VECTOR_KS)
    base = Path(tempfile.mkdtemp(prefix="sre-harness-probe.", dir=args.work_dir))
    try:
        runs = []
        for run in range(1, max(1, args.runs) + 1):
            embedder = Embedder(args.model, args.query_prefix, args.passage_prefix)
            measured = {}
            for size in sizes:
                folder = base / f"run-{run}-size-{size}"
                folder.mkdir()
                measured[str(size)] = probe_size(
                    folder, size, embedder, budgets, vector_ks, args.max_distance, args.rrf_k
                )
            runs.append(measured)
        import sentence_transformers
        import sqlite_vec

        result = {
            "synthetic": True,
            "note": NOTE,
            "model": {**model_card(args.model), "dimension": embedder.dimension},
            "versions": {
                "python": platform.python_version(),
                "sqlite": store.sqlite_version(),
                "sqlite_vec": getattr(sqlite_vec, "__version__", None),
                "sentence_transformers": sentence_transformers.__version__,
            },
            "fusion": {"method": "reciprocal_rank_fusion", "rrf_k": args.rrf_k},
            "vector_ks": list(vector_ks),
            "max_distance": args.max_distance,
            "budgets": list(budgets),
            "runs": len(runs),
            "per_run": runs,
            "summary": summarize(runs),
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(markdown(result["summary"]), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
