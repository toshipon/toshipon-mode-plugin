"""The launcher end to end on synthetic data, exit codes, and what the source must not contain."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from support import EXAMPLES, ROOT, InsightCase, cli, foundation, st

LAUNCHER = ROOT / "scripts" / "insight.py"
RUNTIME = ROOT / "runtime" / "insight_discovery"


class CliCase(InsightCase):
    def cli(self, *args, env=None, now="2026-01-05T09:00:00+00:00"):
        environment = {
            **os.environ,
            "AI_HARNESS_HOME": str(self.home),
            "AI_HARNESS_NOW": now,
            **(env or {}),
        }
        for name in (foundation.ACTIVE_ENV, foundation.AUTOMATED_ENV):
            if name not in (env or {}):
                environment.pop(name, None)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def json(self, *args, **options):
        code, out, err = self.cli(*args, **options)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class EndToEnd(CliCase):
    def setUp(self):
        super().setUp()
        demo = self.json("demo", str(self.tmp / "demo"))
        self.home = st.root(demo["home"]).parent
        self.assertEqual(os.stat(self.home).st_mode & 0o777, 0o700)

    def test_synthetic_run_through_the_agent_adapters(self):
        plan = self.json("run", "--daily", "--dry-run")
        self.assertEqual((plan["estimated_llm_calls"], plan["next"]["fresh"]), (5, 11))
        self.assertFalse(st.ledger_path(self.home).exists())

        summary = self.json("run", "--daily")
        self.assertEqual(summary["llm_calls"], 5)
        self.assertEqual(summary["cycles"][0]["result"], "candidates")
        first, second = summary["cycles"][0]["presented"]
        self.assertEqual(self.json("next"), {"action": "none", "reason": summary["next"]["reason"]})
        self.assertEqual(self.json("run", "--daily")["llm_calls"], 0)

        code, out, _ = self.cli("report")
        self.assertEqual(code, 0)
        self.assertIn("まだ記録がありません", out)
        self.assertNotIn("最小実験", out)
        self.json("prior", "--text", "検査が後ろにありすぎる")
        _, out, _ = self.cli("report")
        self.assertIn("未調査", out)
        self.assertIn("業務 3 種", out)

        weekly = self.json("weekly")
        self.assertEqual(weekly["presented"], [first, second])
        self.assertEqual(self.json("report", "--format", "json")["human_prior_recorded"], True)

        code, _, err = self.cli("select", first, "--reviewer", "claude", "--reason", "r")
        self.assertEqual(code, 3, err)
        code, _, _ = self.cli(
            "select",
            first,
            "--reviewer",
            "Hanako",
            "--reason",
            "r",
            env={"AI_HARNESS_AUTOMATED": "1"},
        )
        self.assertEqual(code, 3)
        self.json("select", first, "--reviewer", "Hanako", "--reason", "今週試す")
        code, _, _ = self.cli("select", second, "--reviewer", "Hanako", "--reason", "これも")
        self.assertEqual(code, 3)
        self.json("resolve", first, "--status", "tested", "--reason", "やり直しが半減した")
        self.json("handoff", second, "--target", "evolution", "--reason", "手順の修正で足りる")

        status = self.json("status")
        self.assertEqual(status["candidates"], {"tested": 1, "candidate": 1})
        self.assertEqual(status["selected_this_week"], first)
        self.assertEqual((status["presented"], status["handoffs"]), ([], 1))
        self.assertTrue(status["latest_report"].endswith("weekly-2026-01-05.md"))
        # The ledger is the foundation's hash chain: reading it back verifies every link.
        records = foundation.read_log(st.ledger_path(self.home))
        self.assertEqual(records[-1]["type"], "handoff_recorded")
        self.assertEqual([record["seq"] for record in records], list(range(1, len(records) + 1)))

    def test_a_run_by_hand_is_held_to_the_daily_cost_cap(self):
        settings = json.loads((st.root(self.home) / "config.json").read_text(encoding="utf-8"))
        paid = {"claude": {"max_budget_usd": 2}}
        settings["agents"] = {"analyst": paid, "auditor": paid}
        foundation.write_json(st.root(self.home) / "config.json", settings)
        foundation.write_json(self.home / "config.json", {"budget": {"max_cost_per_day": 1}})
        summary = self.json("run", "--daily")  # the cap is below one call: nothing is started
        self.assertEqual((summary["llm_calls"], summary["stopped"]["limit"]), (0, "cost"))

    def test_stages_by_hand_and_a_search(self):
        sys.path.insert(0, str(EXAMPLES))
        import stub_agent

        self.json("ingest", "--source", "manual", "--path", str(self.tmp / "demo" / "records"))
        code, _, err = self.cli("step", "prompt", "audit")
        self.assertEqual(code, 1, err)
        for stage in st.STAGES:
            prompt = self.json("step", "prompt", stage)
            self.assertEqual((prompt["stage"], prompt["classification"]), (stage, "synthetic"))
            answer = self.write(f"{stage}.json", stub_agent.answer(prompt["data"]))
            recorded = self.json(
                "step", "record", stage, "--file", str(answer), "--independence", "subagent"
            )
        (name, _) = recorded["cycle"]["presented"]
        code, _, err = self.cli(
            "search", "approve", name, "--query", "ghp_" + "d" * 36, "--reviewer", "Hanako",
            "--reason", "確認",
        )  # fmt: skip
        self.assertEqual(code, 3)
        self.assertNotIn("ghp_", err)
        approved = self.json(
            "search", "approve", name, "--query", "early validation rework", "--reviewer", "Hanako",
            "--reason", "確認",
        )  # fmt: skip
        results = self.write(
            "found.json", {"queries": approved["approved_queries"], "prior_art": []}
        )
        found = self.json("search", "record", name, "--file", str(results))
        self.assertEqual(found["novelty"]["status"], "searched_none_found")


class ExitCodes(CliCase):
    def test_bad_input_is_two_and_refusals_are_three(self):
        code, _, err = self.cli("ingest", "--source", "nowhere")
        self.assertEqual((code, "must be one of" in err), (2, True))
        code, _, _ = self.cli(
            "ingest", "--source", "manual", "--path", str(self.tmp / "a" / ".." / "b")
        )
        self.assertEqual(code, 3)
        code, _, _ = self.cli("step", "record", "observe", "--file", str(self.tmp / "absent.json"))
        self.assertEqual(code, 2)
        self.write("state/insight/config.json", {"limits": {"max_candidates": 9}})
        code, _, err = self.cli("status")
        self.assertEqual((code, "schema violation" in err), (2, True))

    def test_the_shipped_examples_match_their_schemas(self):
        example = json.loads((ROOT / "config" / "insight.example.json").read_text(encoding="utf-8"))
        foundation.validate(f"{foundation.SCHEMAS}:insight-config", example)
        schedule = [
            {"kind": "insight_daily", "hour": 3, "minute": 0},
            {"kind": "insight_weekly", "weekday": 0, "hour": 9, "minute": 30},
        ]
        foundation.validate("config", {"schedule": schedule})
        notes = json.loads((EXAMPLES / "records" / "notes.json").read_text(encoding="utf-8"))
        foundation.validate(f"{foundation.SCHEMAS}:stages#manual_export", notes)

    def test_empty_state_is_a_normal_zero(self):
        self.assertEqual(self.json("next")["action"], "none")
        self.assertEqual(self.json("ingest", "--source", "ledger")["stats"], {})
        self.assertEqual(self.json("status")["candidates"], {})
        code, out, _ = self.cli("report")
        self.assertEqual(code, 0)
        self.assertIn("該当なし", out)

    def test_a_run_already_in_progress_is_skipped_not_failed(self):
        with foundation.locked(st.root(self.home) / "run.lock"):
            self.assertIn("skipped", self.json("run", "--daily"))

    def test_scan_finds_secret_shapes_and_passes_the_fixtures(self):
        self.assertEqual(self.json("scan")["hits"], [])
        leaked = self.write("public/note.md", "token: ghp_" + "e" * 36 + "\n")
        code, _, err = self.cli("scan", str(leaked.parent))
        self.assertEqual(code, 3)
        self.assertIn("github_token", err)

    def test_launcher_says_what_is_missing_without_the_foundation(self):
        lonely = self.tmp / "skills" / "insight-discovery"
        for part in ("scripts", "runtime", "schemas"):
            shutil.copytree(ROOT / part, lonely / part)
        done = subprocess.run(
            [sys.executable, str(lonely / "scripts" / "insight.py"), "next"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 2)
        self.assertIn("Autonomous Improvement", done.stderr)

    def test_launcher_runs_from_its_own_location(self):
        done = subprocess.run(
            [sys.executable, str(LAUNCHER), "next"],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.tmp,
            env={**os.environ, "AI_HARNESS_HOME": str(self.home)},
        )
        self.assertEqual((done.returncode, json.loads(done.stdout)["action"]), (0, "none"))


class SourceBoundaries(unittest.TestCase):
    """What Insight Discovery must not contain: its neighbours' jobs, and ways out of the machine."""

    def sources(self):
        files = [*RUNTIME.rglob("*.py"), LAUNCHER, EXAMPLES / "stub_agent.py"]
        return {path: path.read_text(encoding="utf-8") for path in files}

    def test_the_foundation_is_imported_in_one_place(self):
        for path, text in self.sources().items():
            if path.name != "foundation.py":
                self.assertNotRegex(text, r"(?m)^\s*(from|import) ai_harness", path.name)

    def test_no_second_queue_lock_hash_chain_or_redaction(self):
        forbidden = (
            r"\bfcntl\b|\bflock\b|hashlib|\bhmac\b|os\.replace|mkstemp|lease_expires|backoff"
        )
        for path, text in self.sources().items():
            self.assertNotRegex(text, forbidden, path.name)
        for path, text in self.sources().items():
            if path.name not in ("foundation.py",):
                self.assertNotRegex(text, r"AKIA|sk-ant|ghp_|PRIVATE KEY", path.name)

    def test_nothing_installs_publishes_or_reaches_the_network(self):
        forbidden = (
            r"launchctl|plistlib|crontab|git push|\bgh pr\b|\bgh\s+api|urllib\.request|urlopen"
            r"|\brequests\b|\bhttpx\b|\bsocket\b|http\.client|smtplib|subprocess|os\.system"
        )
        for path, text in self.sources().items():
            self.assertNotRegex(text, forbidden, path.name)

    def test_session_transcripts_are_never_read(self):
        for path, text in self.sources().items():
            self.assertNotRegex(
                text,
                r"\.claude\W{0,8}projects|[\"']projects[\"']|transcript",
                path.name,
            )
        adapter = (RUNTIME / "adapters" / "claude_insights.py").read_text(encoding="utf-8")
        for field in (
            "brief_summary",
            "underlying_goal",
            "first_prompt",
            "project_path",
            '"summary"',
        ):
            self.assertNotIn(field, adapter)
        self.assertNotIn("report.html", re.sub(r'""".*?"""|#.*', "", adapter, flags=re.S))

    def test_evaluation_criteria_are_out_of_reach(self):
        for path, text in self.sources().items():
            self.assertNotRegex(
                text,
                r"evaluation|criteria_change|suite_approve|evolve\b|hypothesis_add|\.approve\(",
                path.name,
            )

    def test_no_model_gets_a_tool(self):
        for role, _, _ in foundation.STAGE_ROLES.values():
            argv = foundation.agents.claude_argv(role, {"max_budget_usd": 0.1})
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertNotIn("--permission-mode", argv)
            schema = json.loads(argv[argv.index("--json-schema") + 1])
            self.assertNotIn("$id", schema)


if __name__ == "__main__":
    unittest.main()
