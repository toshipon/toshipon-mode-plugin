"""Read-only views of the state home, and retention."""

import json
import shutil
from collections import Counter
from datetime import timedelta
from pathlib import Path

from . import evolve, loop
from .jobs import Queue
from .store import HarnessError, parse_time, read_json, read_log, write_private


def overview(home, config):
    runs, corrupt = [], []
    for run_id in loop.list_runs(home):
        try:
            runs.append(loop.status(home, run_id))
        except HarnessError as error:
            corrupt.append({"run_id": run_id, "error": str(error)})
    jobs = Queue(home, config["queue"]).jobs()
    try:
        evolution = evolve.load(home)
    except HarnessError as error:
        corrupt.append({"run_id": "evolution", "error": str(error)})
        evolution = {"hypotheses": {}, "proposals": {}, "suite": None}
    patterns = read_json(evolve.root(home) / "patterns.json", {})
    return {
        "home": str(home),
        "runs": runs,
        "corrupt": corrupt,
        "queue": dict(Counter(job["status"] for job in jobs.values())),
        "dead_jobs": [
            {"id": job["id"], "last_error": job["last_error"]}
            for job in jobs.values()
            if job["status"] == "dead"
        ],
        "patterns": [
            {key: p[key] for key in ("signature", "fresh", "sources", "confidence", "cause_hint")}
            for p in patterns.values()
            if "confidence" in p
        ],
        "evolution": {
            "suite_approved": bool(evolution["suite"]),
            "hypotheses": {key: h["stage"] for key, h in evolution["hypotheses"].items()},
            "awaiting_approval": [
                key
                for key, p in evolution["proposals"].items()
                if p["status"] == "awaiting_approval"
            ],
        },
        "spend": read_json(Path(home) / "spend.json", {}),
    }


def weekly_report(home, config, now):
    view = overview(home, config)
    lines = [f"# 週次レビュー用集計（{now:%Y-%m-%d}）", "", "## 改善ループ", ""]
    for run in view["runs"]:
        outcome = (
            "合格" if run["passed"] else f"未達・{run['next'].get('reason', run['next']['action'])}"
        )
        lines.append(
            f"- {run['run_id']}: {outcome}、試行 {run['iterations']} 回、採否 {run['verdicts']}"
        )
    lines += ["" if view["runs"] else "- 記録なし", "## 繰り返している失敗", ""]
    for pattern in sorted(view["patterns"], key=lambda p: -p["fresh"]):
        lines.append(
            f"- `{pattern['signature']}`: 未処理 {pattern['fresh']} 件、出所 {pattern['sources']}、"
            f"{pattern['confidence']}、原因の候補 {pattern['cause_hint']}"
        )
    lines += ["", "## 人の判断を待っているもの", ""]
    lines += [f"- 提案 {key} の承認" for key in view["evolution"]["awaiting_approval"]]
    lines += [f"- 失敗キューの job {job['id']}: {job['last_error']}" for job in view["dead_jobs"]]
    lines += [f"- 壊れた台帳 {item['run_id']}: {item['error']}" for item in view["corrupt"]]
    lines += ["", f"キュー: {view['queue']}", f"費用: {view['spend']}"]
    return "\n".join(lines) + "\n"


def gc(home, config, now, everything=False):
    """Apply the retention period, or with everything=True delete all recorded state."""
    home = Path(home)
    if everything:
        worktrees = evolve.cleanup(home)
        for child in home.iterdir():
            if child.name == "config.json":
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()  # a link is removed, never followed
            else:
                shutil.rmtree(child)
        return {"deleted": "everything except config.json", "worktrees": worktrees}
    cutoff = now - timedelta(days=config["retention_days"])
    removed = {
        "events": 0,
        "jobs": Queue(home, config["queue"]).purge(cutoff),
        "runs": [],
        "reports": 0,
    }
    events = home / "events.jsonl"
    if events.exists():
        lines = events.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if parse_time(json.loads(line)["created_at"]) >= cutoff]
        removed["events"] = len(lines) - len(kept)
        write_private(events, "".join(line + "\n" for line in kept))
    for run_id in loop.list_runs(home):
        if (home / "runs").is_symlink() or loop.run_dir(home, run_id).is_symlink():
            continue
        try:
            last = read_log(loop.ledger_path(home, run_id))[-1]
        except HarnessError:
            continue  # a corrupt ledger is kept for inspection
        if last["type"] == "run_stopped" and parse_time(last["created_at"]) < cutoff:
            shutil.rmtree(loop.run_dir(home, run_id))
            removed["runs"].append(run_id)
    reports = home / "reports"
    for report in reports.glob("*.md") if reports.is_dir() and not reports.is_symlink() else []:
        if not report.is_symlink() and report.stat().st_mtime < cutoff.timestamp():
            report.unlink()
            removed["reports"] += 1
    return removed
