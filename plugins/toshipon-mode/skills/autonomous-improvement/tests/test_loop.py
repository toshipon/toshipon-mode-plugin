import os
import stat
import unittest
from unittest import mock

from support import CLEAN_AUDIT, HarnessCase, HarnessError, evaluation, git, loop, store


class ImprovementLoopTest(HarnessCase):
    def test_adopted_improvement_passes_only_after_an_independent_audit(self):
        run_id = self.start_run()
        self.assertEqual(self.next(run_id)["action"], "hypothesize")
        decision = self.attempt(run_id, "raise cache_entries to 25", cache_entries=25)
        self.assertEqual(decision["verdict"], "adopt")
        self.assertEqual(decision["diff"]["files"], ["settings.json"])
        self.assertEqual(self.read_settings()["cache_entries"], 25)
        patch = (loop.run_dir(self.home, run_id) / decision["diff"]["patch"]).read_text()
        self.assertIn('+  "cache_entries": 25', patch)

        self.assertEqual(self.next(run_id), {"action": "audit", "triggers": ["completion"]})
        with self.assertRaisesRegex(HarnessError, "does not support stopping as passed"):
            loop.stop(self.home, run_id, "passed", "", self.tick())
        loop.audit_record(self.home, run_id, CLEAN_AUDIT, "same_context", self.tick())
        self.assertEqual(self.next(run_id)["action"], "audit", "a same-context audit is not a gate")
        loop.audit_record(self.home, run_id, CLEAN_AUDIT, "subagent", self.tick())
        self.assertEqual(self.next(run_id)["reason"], "passed")
        self.assertTrue(loop.stop(self.home, run_id, "passed", "", self.tick())["passed"])
        self.assertIn("結果: 合格", loop.report(self.home, run_id))
        with self.assertRaisesRegex(HarnessError, "already stopped"):
            loop.baseline(self.home, run_id, self.tick())

    def test_confirmed_audit_finding_blocks_passing_until_resolved_and_refuted(self):
        run_id = self.start_run()
        self.attempt(run_id, "raise cache_entries to 25", cache_entries=25)
        finding = {
            "id": "F1",
            "severity": "high",
            "category": "evaluation",
            "claim": "throughput is computed from the setting, not measured",
            "evidence": ["EV-0004"],
            "next_experiment": "measure under load",
        }
        audit_id = loop.audit_record(
            self.home, run_id, {"findings": [finding], "limitations": []}, "subagent", self.tick()
        )
        self.assertEqual(self.next(run_id)["action"], "resolve_findings")
        with self.assertRaisesRegex(HarnessError, "needs evidence"):
            loop.audit_resolve(self.home, run_id, audit_id, "F1", "refuted", " ", self.tick())
        loop.audit_resolve(
            self.home, run_id, audit_id, "F1", "confirmed", "read evaluate.py", self.tick()
        )
        step = self.next(run_id)
        self.assertEqual(step["action"], "hypothesize")
        self.assertEqual(step["standing_findings"], ["A-0001/F1"])
        with self.assertRaises(HarnessError):
            loop.stop(self.home, run_id, "passed", "", self.tick())

    def test_regression_is_rejected_and_only_own_changes_are_reverted(self):
        run_id = self.start_run()
        (self.project / "notes.txt").write_text("user work in progress\n")
        decision = self.attempt(run_id, "trade tests for speed", cache_entries=30, tests_pass=False)
        self.assertEqual(decision["verdict"], "reject")
        self.assertEqual(decision["reasons"], ["guard_regression:tests"])
        self.assertTrue(decision["reverted"])
        self.assertEqual(self.read_settings()["cache_entries"], 10)
        self.assertEqual((self.project / "notes.txt").read_text(), "user work in progress\n")

    def test_required_metric_regression_and_new_files_are_reverted(self):
        run_id = self.start_run()
        hypothesis = {
            "statement": "drop validation",
            "rationale": "r",
            "falsification": "f",
            "family": "validation",
        }
        hypothesis_id = loop.hypothesis_add(self.home, run_id, hypothesis, self.tick())
        loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        self.settings(cache_entries=40, valid=False)
        (self.project / "generated.txt").write_text("new file from the experiment\n")
        decision = loop.experiment_finish(self.home, run_id, hypothesis_id, self.tick())
        self.assertEqual(decision["reasons"], ["required_regression:valid"])
        self.assertFalse((self.project / "generated.txt").exists())
        self.assertEqual(git_status(self.project), "")

    def test_touching_the_users_uncommitted_file_needs_a_human_and_is_not_reverted(self):
        run_id = self.start_run()
        self.settings(
            cache_entries=11
        )  # the user's own uncommitted edit, made before the experiment
        decision = self.attempt(run_id, "raise cache_entries to 25", cache_entries=25)
        self.assertEqual(decision["verdict"], "needs_human")
        self.assertFalse(decision["reverted"])
        self.assertEqual(self.read_settings()["cache_entries"], 25)
        self.assertEqual(self.next(run_id)["reason"], "needs_human")
        for reviewer in ("claude", "AI agent", "review-bot", ""):
            with self.assertRaises(HarnessError):
                loop.unblock(self.home, run_id, reviewer, "looks fine", self.tick())
        with mock.patch.dict(os.environ, {store.AUTOMATED_ENV: "1"}):
            with self.assertRaisesRegex(HarnessError, "automated"):
                loop.unblock(self.home, run_id, "Hanako", "checked the diff", self.tick())
        loop.unblock(self.home, run_id, "Hanako", "checked the diff", self.tick())
        self.assertEqual(self.next(run_id)["action"], "baseline")

    def test_plateau_starts_an_audit_then_stops_as_no_improvement(self):
        run_id = self.start_run()
        for index in range(2):
            self.attempt(run_id, f"ineffective variant {index}", family=f"noop-{index}", note=index)
            self.assertEqual(self.next(run_id)["action"], "hypothesize")
            self.assertEqual(self.queue.jobs(), {})
        decision = self.attempt(run_id, "ineffective variant 2", family="noop-2", note=2)
        self.assertEqual(decision["reasons"], ["below_noise"])
        step = self.next(run_id)
        self.assertEqual(step["action"], "audit")
        self.assertEqual(step["triggers"], ["same_failure:below_noise", "no_improvement"])
        self.assertEqual(list(self.queue.jobs()), [f"audit:{run_id}:{decision_seq(self, run_id)}"])
        self.assertEqual(loop.enqueue_due_audits(self.home, self.queue, self.tick()), [])
        with self.assertRaisesRegex(HarnessError, "cannot start an experiment"):
            loop.experiment_start(self.home, run_id, "H-0003", self.tick())

        loop.audit_record(self.home, run_id, CLEAN_AUDIT, "subagent", self.tick())
        self.assertEqual(self.next(run_id)["action"], "hypothesize")
        for index in range(3, 6):
            self.attempt(run_id, f"ineffective variant {index}", family=f"noop-{index}", note=index)
        self.assertEqual(self.next(run_id)["reason"], "no_improvement")
        stopped = loop.stop(self.home, run_id, "no_improvement", "", self.tick())
        self.assertFalse(stopped["passed"])
        self.assertIn("未達（no_improvement）", loop.report(self.home, run_id))

    def test_plateau_thresholds_are_configurable(self):
        run_id = self.start_run(plateau={"same_failure": 2, "no_improvement": 5})
        self.attempt(run_id, "ineffective a", family="a", note="a")
        self.attempt(run_id, "ineffective b", family="b", note="b")
        self.assertEqual(self.next(run_id)["triggers"], ["same_failure:below_noise"])

    def test_budget_exhaustion_stops_the_run_without_calling_it_success(self):
        run_id = self.start_run(budget={"max_iterations": 1})
        self.attempt(run_id, "ineffective", family="noop", note=1)
        self.assertEqual(
            self.next(run_id),
            {"action": "stop", "reason": "budget_exhausted", "detail": "max_iterations"},
        )
        loop.hypothesis_add(
            self.home,
            run_id,
            {"statement": "another", "rationale": "r", "falsification": "f", "family": "other"},
            self.tick(),
        )
        with self.assertRaisesRegex(HarnessError, "budget_exhausted"):
            loop.experiment_start(self.home, run_id, "H-0002", self.tick())
        with self.assertRaises(HarnessError):
            loop.stop(self.home, run_id, "passed", "", self.tick())
        self.assertFalse(
            loop.stop(self.home, run_id, "budget_exhausted", "", self.tick())["passed"]
        )
        self.assertFalse(loop.status(self.home, run_id)["passed"])

    def test_cost_budget_counts_reported_cost(self):
        run_id = self.start_run(budget={"max_iterations": 9, "max_cost": 2})
        self.attempt(run_id, "ineffective a", family="a", note="a")
        self.assertEqual(self.next(run_id)["action"], "hypothesize")
        self.attempt(run_id, "ineffective b", family="b", note="b")
        self.assertEqual(self.next(run_id)["detail"], "max_cost")

    def test_consecutive_unevaluable_experiments_stop_the_run(self):
        run_id = self.start_run()
        for index in range(2):
            decision = self.attempt(
                run_id, f"break evaluator {index}", family=f"b{index}", broken=True
            )
            self.assertEqual(decision["verdict"], "unevaluable")
            self.assertNotIn("broken", self.read_settings())
        self.assertEqual(self.next(run_id)["reason"], "unevaluable")

    def test_duplicate_of_a_rejected_hypothesis_needs_new_evidence(self):
        run_id = self.start_run()
        self.attempt(run_id, "raise cache_entries a little", cache_entries=10)
        again = {
            "statement": "Raise cache_entries a little.",
            "rationale": "r",
            "falsification": "f",
            "family": "cache-size",
        }
        with self.assertRaisesRegex(HarnessError, "duplicate of rejected H-0001"):
            loop.hypothesis_add(self.home, run_id, again, self.tick())
        loop.hypothesis_add(
            self.home,
            run_id,
            {**again, "new_evidence": "profiling shows cache misses"},
            self.tick(),
        )


class CriteriaTest(HarnessCase):
    def test_experiment_that_edits_the_evaluator_is_rejected_and_reverted(self):
        run_id = self.start_run()
        original = (self.project / "evaluate.py").read_text()
        hypothesis_id = loop.hypothesis_add(
            self.home,
            run_id,
            {
                "statement": "inflate the score",
                "rationale": "r",
                "falsification": "f",
                "family": "cheat",
            },
            self.tick(),
        )
        loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        (self.project / "evaluate.py").write_text(original.replace("* 2", "* 200"))
        decision = loop.experiment_finish(self.home, run_id, hypothesis_id, self.tick())
        self.assertEqual(
            (decision["verdict"], decision["reasons"]), ("reject", ["criteria_tamper"])
        )
        self.assertEqual(decision["evaluation"]["status"], "skipped")
        self.assertEqual((self.project / "evaluate.py").read_text(), original)

    def test_criteria_changed_outside_an_experiment_are_refused_until_a_human_approves(self):
        run_id = self.start_run()
        evaluator = self.project / "evaluate.py"
        evaluator.write_text(evaluator.read_text().replace("* 2", "* 200"))
        git(self.project, "commit", "-qam", "loosen the evaluator")
        hypothesis_id = loop.hypothesis_add(
            self.home,
            run_id,
            {"statement": "s", "rationale": "r", "falsification": "f", "family": "x"},
            self.tick(),
        )
        with self.assertRaisesRegex(HarnessError, "criteria changed"):
            loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        with self.assertRaisesRegex(HarnessError, "reviewer must name a person"):
            loop.criteria_change(
                self.home, run_id, self.spec, "Claude", "new evaluator", self.tick()
            )
        loop.criteria_change(self.home, run_id, self.spec, "Hanako", "new load model", self.tick())
        self.assertEqual(self.next(run_id), {"action": "baseline"})
        loop.baseline(self.home, run_id, self.tick())
        self.assertEqual(
            loop.load(self.home, run_id)["best"]["evaluation"]["metrics"]["throughput"]["value"],
            2000,
        )

    def test_spec_rejects_permission_bypass_and_unknown_targets(self):
        self.spec["guard"]["checks"][0]["command"] = [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
        ]
        with self.assertRaisesRegex(HarnessError, "bypasses permission"):
            loop.init(self.home, self.project, self.spec, self.tick(), run_id="run-1")
        self.spec["guard"]["checks"] = []
        self.spec["acceptance"]["targets"][0]["metric"] = "missing"
        with self.assertRaisesRegex(HarnessError, "undeclared metric"):
            loop.init(self.home, self.project, self.spec, self.tick(), run_id="run-1")

    def test_project_must_be_a_git_work_tree(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(HarnessError, "git work tree"):
            loop.init(self.home, plain, self.spec, self.tick())


class LedgerTest(HarnessCase):
    def test_crash_mid_experiment_resumes_from_the_ledger(self):
        run_id = self.start_run()
        hypothesis_id = loop.hypothesis_add(
            self.home,
            run_id,
            {"statement": "raise to 25", "rationale": "r", "falsification": "f", "family": "cache"},
            self.tick(),
        )
        loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        self.settings(cache_entries=25)
        # A new process knows nothing but the ledger.
        self.assertEqual(self.next(run_id), {"action": "finish", "hypothesis_id": hypothesis_id})
        with self.assertRaises(HarnessError):
            loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        decision = loop.experiment_finish(self.home, run_id, hypothesis_id, self.tick())
        self.assertEqual(decision["verdict"], "adopt")

    def test_torn_final_line_is_dropped_and_an_edited_line_is_detected(self):
        run_id = self.start_run()
        path = loop.ledger_path(self.home, run_id)
        with open(path, "ab") as stream:
            stream.write(b'{"schema_version":1,"seq":3,"type":"hypo')  # crash mid-append
        self.assertEqual(len(store.read_log(path)), 2)
        loop.hypothesis_add(
            self.home,
            run_id,
            {"statement": "s", "rationale": "r", "falsification": "f", "family": "x"},
            self.tick(),
        )
        records = store.read_log(path)
        self.assertEqual([record["seq"] for record in records], [1, 2, 3])

        lines = path.read_text().splitlines()
        lines[1] = lines[1].replace('"value":20', '"value":99')
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(HarnessError, "breaks the hash chain"):
            loop.load(self.home, run_id)

    def test_secrets_are_redacted_before_they_reach_the_ledger(self):
        run_id = self.start_run()
        token = "gh" + "p_" + "a" * 36
        loop.hypothesis_add(
            self.home,
            run_id,
            {
                "statement": f"use token {token}",
                "rationale": "pass" + "word=hunter2hunter2",
                "falsification": "f",
                "family": "x",
            },
            self.tick(),
        )
        raw = loop.ledger_path(self.home, run_id).read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn("hunter2hunter2", raw)
        self.assertIn("[REDACTED]", raw)

    def test_state_is_private_and_never_inside_a_repository(self):
        run_id = self.start_run()
        ledger = loop.ledger_path(self.home, run_id)
        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.attempt(run_id, "raise cache_entries to 25", cache_entries=25)
        for path in [self.home, *self.home.rglob("*")]:
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, path)
        with self.assertRaisesRegex(HarnessError, "outside any git repository"):
            store.ensure_home(self.project / ".harness")
        link = self.tmp / "link"
        link.symlink_to(self.home)
        with self.assertRaisesRegex(HarnessError, "symlink"):
            store.ensure_home(link)


def metric(value, n=1, stddev=None, kind="deterministic"):
    return {"value": value, "n": n, "stddev": stddev, "kind": kind, "role": "scored"}


class JudgeTest(unittest.TestCase):
    SPEC = {
        "evaluation": {
            "noise_k": 2,
            "metrics": [
                {
                    "name": "latency",
                    "role": "scored",
                    "kind": "deterministic",
                    "direction": "lower",
                },
                {
                    "name": "quality",
                    "role": "scored",
                    "kind": "llm",
                    "direction": "higher",
                    "noise_floor": 0.5,
                },
            ],
        }
    }

    def judge(self, before, after):
        reference = {
            "evaluation": {"status": "evaluated", "metrics": before, "required": []},
            "guard": [],
        }
        candidate = {"status": "evaluated", "metrics": after, "required": []}
        return evaluation.judge(self.SPEC, reference, candidate, [], [])

    def test_gain_inside_the_measurement_noise_is_not_an_improvement(self):
        quality = metric(3, kind="llm")
        verdict, reasons, measured = self.judge(
            {"latency": metric(100, 5, 10), "quality": quality},
            {"latency": metric(95, 5, 10), "quality": quality},
        )
        self.assertEqual((verdict, reasons), ("reject", ["below_noise"]))
        self.assertAlmostEqual(measured["latency"]["threshold"], 2 * (10**2 / 5 * 2) ** 0.5)
        verdict, _, _ = self.judge(
            {"latency": metric(100, 5, 10), "quality": quality},
            {"latency": metric(80, 5, 10), "quality": quality},
        )
        self.assertEqual(verdict, "adopt")

    def test_llm_score_cannot_carry_an_adoption_but_can_veto_one(self):
        same = metric(100)
        verdict, reasons, _ = self.judge(
            {"latency": same, "quality": metric(3, kind="llm")},
            {"latency": same, "quality": metric(5, kind="llm")},
        )
        self.assertEqual((verdict, reasons), ("reject", ["below_noise"]))
        verdict, reasons, _ = self.judge(
            {"latency": same, "quality": metric(4, kind="llm")},
            {"latency": metric(50), "quality": metric(2, kind="llm")},
        )
        self.assertEqual((verdict, reasons), ("reject", ["side_effect:quality"]))


class MeasureTest(HarnessCase):
    def evaluator(self, body):
        (self.project / "evaluate.py").write_text(f"import json\nprint(json.dumps({body!r}))\n")

    def measure(self, metrics):
        spec = {
            "evaluation": {
                "command": self.spec["evaluation"]["command"],
                "repeats": 2,
                "metrics": metrics,
            }
        }
        return evaluation.measure(spec, self.project, "EV-0001", "baseline", self.clock)

    def test_samples_are_pooled_across_repeats(self):
        self.evaluator({"metrics": {"latency": {"samples": [10, 14]}}})
        result = self.measure(
            [{"name": "latency", "role": "scored", "kind": "deterministic", "direction": "lower"}]
        )
        self.assertEqual(
            (result["metrics"]["latency"]["n"], result["metrics"]["latency"]["value"]), (4, 12)
        )
        self.assertGreater(result["metrics"]["latency"]["stddev"], 0)

    def test_llm_metric_without_rationale_and_missing_metric_are_unevaluable(self):
        llm = [{"name": "quality", "role": "scored", "kind": "llm", "direction": "higher"}]
        self.evaluator({"metrics": {"quality": 4}})
        self.assertEqual(
            self.measure(llm)["unevaluable_reason"], "LLM metric quality has no rationale"
        )
        self.evaluator({"metrics": {"quality": {"value": 4, "rationale": "cites section 2"}}})
        self.assertEqual(self.measure(llm)["metrics"]["quality"]["rationale"], "cites section 2")
        self.evaluator({"metrics": {}})
        self.assertIn("did not report metric", self.measure(llm)["unevaluable_reason"])
        (self.project / "evaluate.py").write_text("print('not json')\n")
        self.assertIn("invalid", self.measure(llm)["unevaluable_reason"])

    def test_timeout_and_missing_command_are_reported_not_raised(self):
        result = evaluation.run_command(
            [self.spec["evaluation"]["command"][0], "-c", "import time; time.sleep(5)"],
            self.project,
            timeout=0.2,
        )
        self.assertIsNone(result["exit_code"])
        self.assertIn("timed out", result["stderr_tail"])
        self.assertIsNone(
            evaluation.run_command(["/nonexistent/evaluator"], self.project)["exit_code"]
        )


def git_status(project):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def decision_seq(case, run_id):
    return loop.load(case.home, run_id)["anchor"]


if __name__ == "__main__":
    unittest.main()
