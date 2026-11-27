"""Build a synthetic review registry (`registry/RG-<n>/v<n>.{json,md}`) for the knowledge index.

Everything is made up: the repositories (`example-org/...`), the people (`alice`, `bob`) and
the rules. Nothing is copied from a real organisation. The same call always writes the same
bytes.

    made = write(state_root)                    # .entries .config, and what each entry becomes
    made = write(state_root, tampered=True)     # plus an approved entry whose artifact changed

Run as a script to write it into the state root ($AI_HARNESS_HOME/sre-harness), with CONFIG as
knowledge/config.json when there is none yet: `python -m examples.synthetic.review_registry`
from `runtime/`.

Entry numbers start at FIRST, far above the `registry/RG-<n>/v0001.md` texts that
`knowledge_corpus.write_sources` writes (those have no `.json` and are no entries), so both
can live under one state root.

    RG-9001  alpha  prose, approved                       -> rule, approved
    RG-9002  alpha  declarative check, approved           -> policy_check, approved, path_globs
    RG-9003  alpha  prose, approved then retired          -> rule, retired
    RG-9004  alpha  prose, rejected                       -> rule, rejected
    RG-9005  gamma  declarative check, approved twice     -> policy_check, approved, version 2
    RG-9006  stray  prose, approved; repo in no project   -> unmapped
    RG-9007  alpha  approved, retired; the retired        -> invalid (hash_mismatch)
                    version's artifact changed afterwards
    RG-9008  alpha  approved; its artifact changed        -> invalid; only with tampered=True,
                    afterwards                               and `registry.active_rules` then
                                                             refuses the repo (exit 3)
"""

import json
from dataclasses import dataclass

from sre_harness import paths, records
from sre_harness.review import candidates, registry

FIRST = 9001
ALPHA = "example-org/alpha"
GAMMA = "example-org/gamma"
STRAY = "example-org/stray"  # in no project of CONFIG
RUN_ID = "run-20261001T000000Z-00009001"
SHA = "0" * 64
CREATED = "2026-10-0{}T00:00:00Z"

CONFIG = {
    "schema_version": 1,
    "projects": {
        "project-alpha": {
            "repos": [ALPHA],
            "default_access": {"classification": "internal", "readers": ["team-alpha"]},
            "components": {"queue": {"path_globs": ["infra/queue/**"]}},
        },
        "project-gamma": {
            "repos": [GAMMA],
            "default_access": {"classification": "confidential", "readers": ["team-gamma"]},
            "components": {},
        },
    },
    "principals": {
        "workspace-a": {
            "readers": ["everyone", "team-alpha", "team-gamma"],
            "classifications": ["public", "synthetic", "internal", "confidential"],
        },
        "workspace-b": {
            "readers": ["everyone", "team-alpha"],
            "classifications": ["public", "synthetic", "internal"],
        },
    },
}

QUEUE_CHECK = {
    "path_globs": ["infra/queue/**"],
    "added_line_pattern": "message_retention_seconds",
    "absent_pattern": "1209600",
    "message": "キューのメッセージ保持期間は 14 日にする",
}
TIMEOUT_CHECK = {
    "path_globs": ["src/**/*.py"],
    "added_line_pattern": "requests\\.(get|post)\\(",
    "absent_pattern": "timeout=",
    "message": "外向きの HTTP 呼び出しには timeout を設定する",
}


@dataclass(frozen=True)
class Scenario:
    config: dict
    entries: dict  # entry id -> {repo, statuses, title, form}
    approved: dict  # repo -> entry ids whose latest version is approved
    unmapped: list
    invalid: list


def text(title, body, check=None):
    return candidates.render(title, body, check)


def version(entry_id, number, status, repo, artifact, previous=None):
    """One review-registry version record for `artifact` (parsed as approve records it)."""
    title, check, problems = candidates.parse(artifact)
    assert not problems, problems
    block = {
        "content_sha256": records.sha256(artifact),
        "form": "prose" if check is None else "declarative_check",
        "title": title,
        "artifact_file": registry.version_name(number, "md"),
    }
    if check is not None:
        block["check"] = check
    approved = status == "approved"
    return {
        "schema_version": 1,
        "id": entry_id,
        "version": number,
        "created_at": CREATED.format(number),
        "status": status,
        "repo": repo,
        "lineage": {"run_id": RUN_ID, "candidate_id": "IC-" + entry_id.split("-")[1]},
        "candidate": block,
        "evaluation": (
            {
                "ref": f"runs/{RUN_ID}/evaluation.json",
                "sha256": SHA,
                "criteria_hash": SHA,
                "status": "evaluated",
                "content_sha256": block["content_sha256"],
            }
            if approved
            else None
        ),
        "decision": {
            "reviewer": "alice" if approved else "bob",
            "reason": "合成データの判断",
            "decided_at": CREATED.format(number),
            "via": "terminal_hash_prefix" if approved else "command",
        },
        "previous_version": previous,
        "activation": None,
    }


def write_entry(root, entry_id, repo, history):
    """Write the versions of one entry: `history` is [(status, artifact text)], oldest first."""
    for number, (status, artifact) in enumerate(history, start=1):
        record = version(entry_id, number, status, repo, artifact, number - 1 or None)
        registry.write_version(root, record, artifact)


def tamper(root, entry_id, number):
    """Change a version's artifact after it was recorded (its hash no longer matches)."""
    path = paths.child(root, registry.DIRECTORY, entry_id, registry.version_name(number, "md"))
    paths.write_private(path, path.read_text(encoding="utf-8") + "\n書き換えた行\n")


def entry_id(offset):
    return f"RG-{FIRST + offset:04d}"


def write(root, tampered=False):
    """Write the synthetic registry under the state root `root`. Returns a Scenario."""
    http = text(
        "外向きの HTTP 呼び出しには timeout を設定する", "接続と読み取りの両方に上限を置く。"
    )
    queue = text("キューの保持期間は 14 日にする", "保持期間を明示する。", QUEUE_CHECK)
    retired = text("ログの保持期間は 30 日にする", "古い規則。")
    rejected = text("すべての API に認証を付けない", "却下した提案。")
    first = text("HTTP の timeout を検査する", "宣言的検査の初版。", TIMEOUT_CHECK)
    second = text("HTTP の timeout を検査する（改訂）", "宣言的検査の改訂版。", TIMEOUT_CHECK)
    stray = text("設定に無い repo の規則", "どの project にも属さない。")
    changed = text("後から書き換えた規則", "取り下げた版の artifact が変わる。")
    plan = [
        (ALPHA, [("approved", http)]),
        (ALPHA, [("approved", queue)]),
        (ALPHA, [("approved", retired), ("retired", retired)]),
        (ALPHA, [("rejected", rejected)]),
        (GAMMA, [("approved", first), ("approved", second)]),
        (STRAY, [("approved", stray)]),
        (ALPHA, [("approved", changed), ("retired", changed)]),
    ]
    if tampered:
        plan.append((ALPHA, [("approved", text("承認後に書き換えた規則", "本文。"))]))
    entries, approved = {}, {}
    for offset, (repo, history) in enumerate(plan):
        name = entry_id(offset)
        write_entry(root, name, repo, history)
        statuses = [status for status, _ in history]
        title, check, _ = candidates.parse(history[-1][1])
        entries[name] = {
            "repo": repo,
            "statuses": statuses,
            "title": title,
            "form": "prose" if check is None else "declarative_check",
        }
        if statuses[-1] == "approved":
            approved.setdefault(repo, []).append(name)
    tamper(root, entry_id(6), 2)
    invalid = [entry_id(6)]
    if tampered:
        tamper(root, entry_id(7), 1)
        invalid.append(entry_id(7))
    return Scenario(
        config=CONFIG,
        entries=entries,
        approved=approved,
        unmapped=[entry_id(5)],
        invalid=invalid,
    )


def main():
    root = paths.state_root()
    target = paths.child(root, "knowledge", "config.json")
    if not target.exists():
        paths.write_private(target, records.canonical(CONFIG) + "\n")
    made = write(root)
    print(json.dumps({"registry": sorted(made.entries), "config": str(target)}))


if __name__ == "__main__":
    main()
