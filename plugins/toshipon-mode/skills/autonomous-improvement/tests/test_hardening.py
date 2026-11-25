"""Regression tests for defects found by the independent review of the first implementation."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from support import (
    CLEAN_AUDIT,
    EvolutionCase,
    HarnessCase,
    HarnessError,
    agents,
    dispatch,
    evaluation,
    evolve,
    git,
    loop,
    record,
    status,
    store,
)
from support import redact as redaction

BETTER = "# Synthetic working skill\n\n- 変更したらテストを実行する。\n- 完了報告に evidence を添える。\n"


class GuardHardeningTest(HarnessCase):
    def test_revert_treats_paths_literally_and_spares_the_users_dirty_file(self):
        (self.project / "[id].tsx").write_text("orig id\n")
        (self.project / "d.tsx").write_text("orig d\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "routes")
        run_id = self.start_run()
        (self.project / "d.tsx").write_text("USER WORK IN PROGRESS\n")
        hypothesis_id = loop.hypothesis_add(
            self.home,
            run_id,
            {
                "statement": "edit the route",
                "rationale": "r",
                "falsification": "f",
                "family": "route",
            },
            self.tick(),
        )
        loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        (self.project / "[id].tsx").write_text("changed by the experiment\n")
        self.settings(tests_pass=False)
        decision = loop.experiment_finish(self.home, run_id, hypothesis_id, self.tick())
        self.assertEqual(decision["verdict"], "reject")
        self.assertEqual((self.project / "d.tsx").read_text(), "USER WORK IN PROGRESS\n")
        self.assertEqual((self.project / "[id].tsx").read_text(), "orig id\n")
        patch = (loop.run_dir(self.home, run_id) / decision["diff"]["patch"]).read_text()
        self.assertNotIn("d.tsx", patch)

    def test_an_experiment_that_changed_nothing_is_never_adopted(self):
        run_id = self.start_run(
            acceptance={"targets": [{"metric": "throughput", "op": ">=", "value": 1000}]}
        )
        self.settings(cache_entries=11)  # the user's uncommitted edit
        self.attempt(
            run_id, "raise cache_entries to 25", cache_entries=25
        )  # needs_human, left in place
        loop.unblock(self.home, run_id, "Hanako", "kept the change", self.tick())
        self.assertEqual(
            self.next(run_id), {"action": "baseline"}, "the reference is measured again"
        )
        loop.baseline(self.home, run_id, self.tick())
        decision = self.attempt(run_id, "take the credit without a change", family="noop")
        self.assertEqual((decision["verdict"], decision["reasons"]), ("reject", ["no_change"]))

    def test_evaluator_paths_must_be_plain_relative_paths(self):
        for field, value in (
            ("files", "./evaluate.py"),
            ("files", "../evaluate.py"),
            ("files", "/etc/hosts"),
        ):
            spec = json.loads(json.dumps(self.spec))
            spec["evaluation"][field] = [value]
            with self.assertRaisesRegex(HarnessError, "plain relative path"):
                loop.init(self.home, self.project, spec, self.tick(), run_id="run-x")
        self.spec["guard"]["protected_paths"] = ["./check.py"]
        with self.assertRaisesRegex(HarnessError, "plain relative path"):
            loop.init(self.home, self.project, self.spec, self.tick(), run_id="run-x")

    def test_evaluator_changed_where_git_cannot_see_it_closes_the_experiment_as_tamper(self):
        (self.project / ".gitignore").write_text("weights.json\n")
        (self.project / "weights.json").write_text("{}\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "ignore weights")
        self.spec["evaluation"]["files"] = ["evaluate.py", "weights.json"]
        run_id = self.start_run()
        hypothesis_id = loop.hypothesis_add(
            self.home,
            run_id,
            {"statement": "tune", "rationale": "r", "falsification": "f", "family": "weights"},
            self.tick(),
        )
        loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        self.settings(cache_entries=25)
        (self.project / "weights.json").write_text('{"bonus": 100}\n')
        decision = loop.experiment_finish(self.home, run_id, hypothesis_id, self.tick())
        self.assertEqual(
            (decision["verdict"], decision["reasons"]), ("reject", ["criteria_tamper"])
        )
        self.assertEqual(self.read_settings()["cache_entries"], 10)
        self.assertEqual(
            self.next(run_id)["action"], "hypothesize", "the run is not stuck on finish"
        )

    def test_unevaluable_streak_does_not_survive_a_criteria_change(self):
        run_id = self.start_run()
        for index in range(2):
            self.attempt(run_id, f"break evaluator {index}", family=f"b{index}", broken=True)
        self.assertEqual(self.next(run_id)["reason"], "unevaluable")
        loop.criteria_change(
            self.home, run_id, self.spec, "Hanako", "fixed the evaluator", self.tick()
        )
        loop.baseline(self.home, run_id, self.tick())
        self.assertEqual(self.next(run_id)["action"], "hypothesize")

    def test_relative_min_effect_still_applies_when_the_reference_is_zero(self):
        spec = {
            "evaluation": {
                "metrics": [
                    {
                        "name": "hits",
                        "role": "scored",
                        "kind": "deterministic",
                        "direction": "higher",
                        "min_effect": 0.05,
                    }
                ]
            }
        }
        zero = {"value": 0.0, "n": 1, "stddev": None, "kind": "deterministic", "role": "scored"}
        measured = evaluation.compare(
            spec, {"metrics": {"hits": zero}}, {"metrics": {"hits": {**zero, "value": 1e-9}}}
        )
        self.assertEqual(measured["hits"]["effect"], "none")

    def test_spec_with_a_credential_is_refused_and_token_counts_are_not_mangled(self):
        self.spec["evaluation"]["command"] += ["--max_tokens=100000"]
        (self.project / "evaluate.py").write_text(
            (self.project / "evaluate.py")
            .read_text()
            .replace("import json", "import json  # accepts flags")
        )
        git(self.project, "commit", "-qam", "flags")
        run_id = self.start_run()
        self.assertIn(
            "--max_tokens=100000", loop.load(self.home, run_id)["spec"]["evaluation"]["command"]
        )
        self.spec["evaluation"]["command"][-1] = "--api-" + "key=" + "sk-" + "a" * 24
        with self.assertRaisesRegex(HarnessError, "secret-shaped"):
            loop.init(self.home, self.project, self.spec, self.tick(), run_id="run-2")

    def test_stop_from_a_subdirectory_still_queues_the_due_audit(self):
        run_id = self.start_run()
        self.attempt(run_id, "raise cache_entries to 25", cache_entries=25)
        other = store.DEFAULT_CONFIG["queue"]
        queue = type(self.queue)(store.ensure_home(self.tmp / "other-state"), other)
        (self.project / "src").mkdir()
        self.assertEqual(
            loop.enqueue_due_audits(self.home, queue, self.tick(), project=self.project / "src"),
            [f"audit:{run_id}:{loop.load(self.home, run_id)['anchor']}"],
        )


class RedactionTest(unittest.TestCase):
    def test_credential_shapes_are_found_and_removed(self):
        leaks = {
            "json": '{"pass' + 'word": "hunter2hunter2"}',
            "basic": "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
            "url": "https://deploy:S3cr3tPassw0rd@git.example.invalid/repo.git",
            "quoted": "pass" + "word = 'correct horse battery staple'",
            "env": "DB_PASS=Pa55word99",
            "short": "pass" + "word=abc12",
            "session_key": "AS" + "IA" + "B" * 16,
            "stripe": "sk_" + "live_" + "c" * 24,
            "jwt": "ey" + "J" + "a" * 20 + ".ey" + "J" + "b" * 20 + "." + "c" * 20,
        }
        for name, text in leaks.items():
            with self.subTest(name):
                self.assertTrue(redaction.find_secrets(text))
                cleaned = redaction.redact_text(text)
                for fragment in (
                    "hunter2",
                    "dXNlcjpw",
                    "S3cr3t",
                    "horse battery",
                    "Pa55word",
                    "abc12",
                    "B" * 16,
                    "c" * 20,
                ):
                    self.assertNotIn(fragment, cleaned)
        begin, end = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
        key = f"{begin}\nMIIEowIBAAKCAQEA1234\nabcd\n{end}"
        self.assertEqual(
            redaction.redact_text(f"before\n{key}\nafter"), "before\n[REDACTED]\nafter"
        )

    def test_ordinary_text_is_left_alone(self):
        for text in (
            "--max_tokens=100000",
            "tokens: 123456",
            "token_budget: 200000",
            "passwordless=enabled",
            "bypass: true",
            "https://example.invalid/path?x=1",
            "see user@example.invalid",
        ):
            with self.subTest(text):
                self.assertEqual(redaction.find_secrets(text), [])
                self.assertEqual(redaction.redact_text(text), text)


class HumanGateTest(unittest.TestCase):
    def test_reviewer_names_are_split_on_case_and_digits(self):
        for human in ("Claudette", "Aiko", "Abbot", "Hanako Yamada"):
            self.assertEqual(store.require_human(human, "reason", env={})["reviewer"], human)
        for machine in (
            "ClaudeCode",
            "GPT4",
            "claude2",
            "OpenAIBot",
            "CodexAgent",
            "AI",
            "review-bot",
        ):
            with self.subTest(machine), self.assertRaises(HarnessError):
                store.require_human(machine, "reason", env={})


class AdapterHardeningTest(HarnessCase):
    def setUp(self):
        super().setUp()
        self.config = store.load_config(self.home)
        self.config["agents"]["auditor"] = {"claude": {"max_budget_usd": 1.5}}
        self.budget = agents.Budget(self.home, self.clock, cap=4)

    def call(self, stdout, code=0):
        done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
        with mock.patch.object(evaluation.subprocess, "run", return_value=done) as run:
            try:
                return agents.invoke(
                    self.config, "auditor", {}, self.tmp, "synthetic", budget=self.budget
                ), run
            except HarnessError as error:
                return error, run

    def test_cost_is_charged_even_when_the_adapter_output_is_rejected(self):
        envelope = {
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 2.5,
            "structured_output": {"bad": 1},
        }
        error, _ = self.call(json.dumps(envelope))
        self.assertIn("schema violation", str(error))
        self.assertEqual(self.budget.spent(), 2.5)

    def test_a_failed_call_is_charged_its_whole_allowance_and_the_cap_is_checked_first(self):
        error, _ = self.call("", code=1)
        self.assertIn("exit code 1", str(error))
        self.assertEqual(self.budget.spent(), 1.5)
        self.call("", code=1)
        error, run = self.call("", code=1)
        self.assertIsInstance(error, agents.BudgetExceeded)
        run.assert_not_called()
        self.assertEqual(self.budget.spent(), 3.0)

    def test_claude_adapter_must_declare_a_budget(self):
        with self.assertRaisesRegex(HarnessError, "max_budget_usd"):
            store.validate("config", {"agents": {"auditor": {"claude": {"model": "opus"}}}})

    def test_agent_adapters_get_a_minimal_environment_but_evaluators_keep_theirs(self):
        script = "import os, json; print(json.dumps({'findings': [], 'limitations': [os.environ.get('DEPLOY_TOKEN', 'absent')]}))"
        self.config["agents"]["auditor"] = {"argv": [sys.executable, "-c", script]}
        with mock.patch.dict(os.environ, {"DEPLOY_TOKEN": "visible"}):
            (output, _) = agents.invoke(
                self.config, "auditor", {}, self.tmp, "synthetic", budget=self.budget
            )
            shown = evaluation.run_command(
                [sys.executable, "-c", "import os; print(os.environ['DEPLOY_TOKEN'])"], self.tmp
            )
        self.assertEqual(output["limitations"], ["absent"])
        self.assertEqual(shown["stdout"].strip(), "visible")

    def test_direct_claude_commands_may_not_loosen_permissions(self):
        for argv in (
            ["claude", "-p", "--permission-mode", "auto"],
            ["/opt/bin/claude", "-p", "--settings", "/tmp/perm.json"],
            ["claude", "-p", "--allowedTools", "Bash(*)"],
            ["claude", "-p", "--allowed-tools=Bash"],
        ):
            with self.subTest(argv), self.assertRaisesRegex(HarnessError, "permission"):
                evaluation.assert_safe_argv(argv)
        for role in agents.ROLES:
            evaluation.assert_safe_argv(agents.claude_argv(role, {"max_budget_usd": 1}))

    def test_hook_tool_names_are_reduced_to_a_safe_alphabet(self):
        payload = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "s",
            "cwd": "/w",
            "tool_use_id": "t",
            "tool_name": "x` [click](https://evil.invalid) `",
        }
        event = record.build_event(payload, self.config, self.clock)
        self.assertEqual(event["tool_name"], "unknown")
        self.assertEqual(
            record.build_event(
                {**payload, "tool_name": "mcp__files__read"}, self.config, self.clock
            )["tool_name"],
            "mcp__files__read",
        )


class QueueHardeningTest(EvolutionCase):
    def test_released_job_honours_a_cancel_request(self):
        self.queue.enqueue("job", "evolve", {}, self.clock)
        self.queue.claim(self.clock)
        self.queue.cancel("job", self.clock)
        self.queue.release("job", self.clock, "deferred")
        self.assertEqual(self.queue.jobs()["job"]["status"], "cancelled")
        self.assertIsNone(self.queue.claim(self.tick()))

    def test_job_results_are_redacted(self):
        self.queue.enqueue("job", "evolve", {}, self.clock)
        self.queue.claim(self.clock)
        self.queue.complete(
            "job", self.clock, {"reason": "saw api_" + "token=abcdef1234567890 in the logs"}
        )
        self.assertNotIn("abcdef1234567890", json.dumps(self.queue.jobs()))

    def test_exhausted_cost_budget_defers_agent_jobs_without_starving_the_rest(self):
        self.queue.enqueue("evolve:a", "evolve", {}, self.tick())
        self.queue.enqueue("weekly:a", "weekly_report", {}, self.tick())
        self.write_config(budget={"max_cost_per_day": 1})
        now = self.tick()
        agents.Budget(self.home, now, cap=1).charge(1.5)
        results = dispatch.dispatch(self.home, self.config, clock=lambda: now)
        self.assertEqual(
            [(r["id"], r["status"]) for r in results],
            [("evolve:a", "deferred"), ("weekly:a", "done")],
        )
        self.assertEqual(self.queue.jobs()["evolve:a"]["attempts"], 0)


class EvolutionHardeningTest(EvolutionCase):
    def setUp(self):
        super().setUp()
        self.approve_suite()
        self.tool_failures()

    def freeze(self, change):
        evolve.analyze(self.home, self.config)
        hypothesis_id = evolve.hypothesis_add(
            self.home,
            {
                "pattern": "tool_failure:Bash",
                "cause": "skill",
                "statement": "s",
                "falsification": "f",
            },
            self.tick(),
        )
        worktree = evolve.candidate_create(self.home, hypothesis_id)
        change(worktree)
        return hypothesis_id, evolve.candidate_freeze(self.home, hypothesis_id, self.tick())

    def approved_proposal(self, change, allow):
        hypothesis_id, _ = self.freeze(change)
        evolve.compare(self.home, hypothesis_id, self.tick())
        evolve.audit_record(self.home, hypothesis_id, CLEAN_AUDIT, "subagent", self.tick())
        evolve.propose(self.home, hypothesis_id, self.tick())
        evolve.approve(self.home, "P-0001", "Hanako", "read it", self.tick())
        return self.write_config(publish={"allow_paths": allow})

    def test_failed_registration_does_not_consume_the_evidence(self):
        self.edit_suite(lambda suite: suite["subject"].update(baseline_ref="no-such-ref"))
        self.approve_suite()
        evolve.analyze(self.home, self.config)
        with self.assertRaisesRegex(HarnessError, "rev-parse"):
            evolve.hypothesis_add(
                self.home,
                {
                    "pattern": "tool_failure:Bash",
                    "cause": "skill",
                    "statement": "s",
                    "falsification": "f",
                },
                self.tick(),
            )
        self.assertEqual(evolve.analyze(self.home, self.config)["tool_failure:Bash"]["fresh"], 3)

    def test_candidate_file_list_is_exact_for_spaces_non_ascii_renames_and_deletions(self):
        def change(worktree):
            (worktree / "docs").mkdir()
            (worktree / "docs" / "my guide.md").write_text("guide\n")
            (worktree / "docs" / "日本語.md").write_text("文書\n")
            (worktree / "skill.md").rename(worktree / "docs" / "skill.md")

        _, frozen = self.freeze(change)
        self.assertEqual(
            frozen["files"], ["docs/my guide.md", "docs/skill.md", "docs/日本語.md", "skill.md"]
        )

    def test_publish_gate_blocks_paths_outside_the_allowlist_including_the_source_of_a_move(self):
        config = self.approved_proposal(
            lambda worktree: (
                (worktree / "skill.md").write_text(BETTER),
                (worktree / "docs").mkdir(),
                (worktree / "docs" / "deep").mkdir(),
                (worktree / "docs" / "deep" / "note.md").write_text("note\n"),
            ),
            ["skill.md", "docs/*"],
        )
        with self.assertRaisesRegex(
            HarnessError, "not on the publish allowlist: docs/deep/note.md"
        ):
            evolve.publish_check(self.home, config, "P-0001")
        evolve.publish_check(
            self.home, self.write_config(publish={"allow_paths": ["skill.md", "docs/**"]}), "P-0001"
        )

    def test_publish_gate_blocks_symlinks_and_mode_changes(self):
        def change(worktree):
            (worktree / "skill.md").write_text(BETTER)
            os.chmod(worktree / "skill.md", 0o755)
            os.symlink("../../outside", worktree / "link.md")

        config = self.approved_proposal(change, ["*"])
        with self.assertRaises(HarnessError) as blocked:
            evolve.publish_check(self.home, config, "P-0001")
        self.assertIn("symbolic link", str(blocked.exception))
        self.assertIn("file mode change", str(blocked.exception))

    def test_publish_gate_scans_json_credentials_and_connection_strings(self):
        def change(worktree):
            (worktree / "skill.md").write_text(
                BETTER + "- db: postgres://app:Pa55word99@db.example.invalid/prod\n"
            )

        with self.assertRaisesRegex(HarnessError, "secret-shaped"):
            self.freeze(change)

    def test_holdout_check_matches_whole_case_ids(self):
        evolve.analyze(self.home, self.config)
        base = {"pattern": "tool_failure:Bash", "cause": "skill", "falsification": "f"}
        evolve.hypothesis_add(
            self.home, {**base, "statement": "improve holdout-claims handling"}, self.tick()
        )
        self.tool_failures(count=3, tool="Edit")
        evolve.analyze(self.home, self.config)
        with self.assertRaisesRegex(HarnessError, "cites holdout cases"):
            evolve.hypothesis_add(
                self.home,
                {**base, "pattern": "tool_failure:Edit", "statement": "fix (holdout-claim)"},
                self.tick(),
            )

    def test_suite_must_keep_its_files_inside_itself_and_outside_the_subject_repository(self):
        self.edit_suite(lambda suite: suite["cases"][0].update(path="/etc/hosts"))
        with self.assertRaisesRegex(HarnessError, "inside the suite directory"):
            self.approve_suite()
        self.edit_suite(lambda suite: suite["cases"][0].update(path="../project/settings.json"))
        with self.assertRaisesRegex(HarnessError, "inside the suite directory"):
            self.approve_suite()
        self.edit_suite(
            lambda suite: suite["cases"][0].update(path="cases/train-missing-evidence.json")
        )
        self.edit_suite(lambda suite: suite["subject"].update(repo=".."))
        git(self.tmp, "init", "-q")
        with self.assertRaisesRegex(HarnessError, "outside the subject repository"):
            self.approve_suite()

    def test_git_children_carry_the_recursion_guard_and_run_no_repository_hooks(self):
        marker = self.tmp / "hook-ran"
        hook = self.subject / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
        hook.chmod(0o755)
        self.freeze(lambda worktree: (worktree / "skill.md").write_text(BETTER))
        self.assertFalse(marker.exists())

    def test_patterns_carry_the_classification_of_their_sources(self):
        patterns = evolve.analyze(self.home, self.config)
        self.assertEqual(patterns["tool_failure:Bash"]["classification"], "internal")
        self.write_config(llm_policy={"allowed_classifications": ["public", "synthetic"]})
        with self.assertRaisesRegex(HarnessError, "classified internal may not be sent"):
            evolve.daily(self.home, self.config, self.tick())

    def test_cleanup_and_gc_do_not_follow_symlinks_out_of_the_state_home(self):
        outside = self.tmp / "users-worktree"
        git(self.subject, "worktree", "add", "-q", "--detach", str(outside))
        (outside / "WIP.txt").write_text("unsaved\n")
        worktrees = store.private_dir(evolve.root(self.home) / "worktrees")
        (worktrees / "EH-0009").symlink_to(outside)
        self.assertEqual(evolve.cleanup(self.home), [])
        self.assertTrue((outside / "WIP.txt").exists())

        notes = self.tmp / "my-notes"
        notes.mkdir()
        (notes / "keep.md").write_text("mine\n")
        os.utime(notes / "keep.md", (0, 0))
        (self.home / "reports").symlink_to(notes)
        status.gc(self.home, self.config, self.tick())
        status.gc(self.home, self.config, self.tick(), everything=True)
        self.assertTrue((notes / "keep.md").exists())
        self.assertTrue((outside / "WIP.txt").exists())

    def test_dry_run_does_not_write_through_symlinks_in_the_state_home(self):
        real = self.tmp / "real-reports"
        real.mkdir()
        (self.home / "reports").symlink_to(real)
        dispatch.dry_run(self.home, self.tick())
        self.assertEqual(list(real.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
