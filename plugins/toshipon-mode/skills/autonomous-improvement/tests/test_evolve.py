import json
import os
import subprocess
import unittest
from unittest import mock

from support import CLEAN_AUDIT, EvolutionCase, HarnessError, evolve, store

BETTER = "# Synthetic working skill\n\n- 変更したらテストを実行する。\n- 完了報告に evidence を添える。\n"
NO_TITLE = (
    "Synthetic working skill\n\n- 変更したらテストを実行する。\n- 完了報告に evidence を添える。\n"
)
UNRELATED = "# Synthetic working skill\n\n- 変更したらテストを実行する。\n- 挨拶を丁寧にする。\n"


def worktrees(repo):
    listing = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listing.stdout.count("worktree ")


class AnalysisTest(EvolutionCase):
    def test_a_single_failure_or_a_single_source_is_not_generalized(self):
        self.approve_suite()
        self.tool_failures(count=1)
        patterns = evolve.analyze(self.home, self.config)
        self.assertEqual(patterns["tool_failure:Bash"]["confidence"], "candidate")
        self.assertEqual(evolve.daily(self.home, self.config, self.tick())["action"], "none")

        self.tool_failures(count=5, projects=("/work/alpha",), tool="Edit")
        patterns = evolve.analyze(self.home, self.config)
        self.assertEqual(
            (patterns["tool_failure:Edit"]["fresh"], patterns["tool_failure:Edit"]["sources"]),
            (5, 1),
        )
        self.assertEqual(evolve.eligible(patterns), [])
        with self.assertRaisesRegex(HarnessError, "single failure is not generalized"):
            evolve.hypothesis_add(
                self.home,
                {
                    "pattern": "tool_failure:Edit",
                    "cause": "tool",
                    "statement": "s",
                    "falsification": "f",
                },
                self.tick(),
            )
        self.assertEqual(evolve.load(self.home)["hypotheses"], {})

    def test_failures_recorded_by_improvement_runs_feed_the_patterns(self):
        run_id = self.start_run()
        for index in range(3):
            self.attempt(run_id, f"ineffective {index}", family=f"f{index}", note=index)
        patterns = evolve.analyze(self.home, self.config)
        pattern = patterns["decision:below_noise"]
        self.assertEqual(
            (pattern["fresh"], pattern["sources"], pattern["cause_hint"]), (3, 1, "instruction")
        )
        self.assertEqual(evolve.analyze(self.home, self.config)["decision:below_noise"]["fresh"], 3)


class MetaLoopTest(EvolutionCase):
    def test_recurring_failure_becomes_a_measured_audited_local_proposal(self):
        self.approve_suite()
        self.tool_failures()
        before = (self.subject / "skill.md").read_text()
        result = evolve.daily(self.home, self.config, self.tick())
        self.assertEqual(result["action"], "proposed")
        self.assertEqual(
            (self.subject / "skill.md").read_text(), before, "the user's checkout is untouched"
        )

        folder = evolve.root(self.home) / "proposals" / "P-0001"
        proposal = json.loads((folder / "proposal.json").read_text())
        store.validate("proposal", proposal)
        head = subprocess.run(
            ["git", "-C", str(self.subject), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(proposal["baseline_sha"], head)
        self.assertEqual(proposal["diff"]["files"], ["skill.md"])
        self.assertEqual(len(proposal["hypothesis"]["evidence"]), 3)
        holdout = proposal["measurements"]["metrics"]
        self.assertAlmostEqual(holdout["baseline"]["holdout"]["first_pass_rate"]["mean"], 1 / 3)
        self.assertEqual(
            holdout["candidate"]["holdout"]["first_pass_rate"], {"mean": 1.0, "stddev": 0.0, "n": 3}
        )
        self.assertEqual(holdout["candidate"]["train"]["failure_recurrence_rate"]["mean"], 0.0)
        report = (folder / "report.md").read_text()
        for expected in (
            "## 差分",
            "## 測定結果",
            "## リスク",
            "## ロールバック",
            "git revert",
            "first_pass_rate | holdout",
        ):
            self.assertIn(expected, report)

        again = evolve.daily(self.home, self.config, self.tick())
        self.assertEqual(again["action"], "none", "the same evidence is not proposed twice")

    def test_publishing_needs_a_recorded_human_approval_and_passes_the_allowlist(self):
        self.approve_suite()
        self.tool_failures()
        evolve.daily(self.home, self.config, self.tick())
        with self.assertRaisesRegex(HarnessError, "no recorded human approval"):
            evolve.publish_check(self.home, self.config, "P-0001")
        for reviewer in ("claude", "harness-bot"):
            with self.assertRaises(HarnessError):
                evolve.approve(self.home, "P-0001", reviewer, "ok", self.tick())
        with mock.patch.dict(os.environ, {store.AUTOMATED_ENV: "1"}):
            with self.assertRaisesRegex(HarnessError, "automated"):
                evolve.approve(self.home, "P-0001", "Hanako", "measured", self.tick())
        evolve.approve(self.home, "P-0001", "Hanako", "差分と測定を読んだ", self.tick())
        plan = evolve.publish_check(self.home, self.config, "P-0001")
        self.assertEqual(plan["approved_by"], "Hanako")
        self.assertIn("git switch -c harness-evolution/p-0001", plan["commands"][0])

        with self.assertRaisesRegex(HarnessError, "not on the publish allowlist: skill.md"):
            evolve.publish_check(
                self.home, self.write_config(publish={"allow_paths": ["docs/*"]}), "P-0001"
            )
        terms = self.tmp / "deny.txt"
        terms.write_text("# client names\nEVIDENCE\n")
        config = self.write_config(
            publish={"allow_paths": ["skill.md"]}, privacy={"deny_terms_file": str(terms)}
        )
        with self.assertRaises(HarnessError) as blocked:
            evolve.publish_check(self.home, config, "P-0001")
        self.assertIn("forbidden term #1", str(blocked.exception))
        self.assertNotIn("evidence", str(blocked.exception).lower())

        patch = evolve.root(self.home) / "proposals" / "P-0001" / "candidate.patch"
        patch.write_text(patch.read_text() + "+extra line after approval\n")
        with self.assertRaisesRegex(HarnessError, "differs from the one that was measured"):
            evolve.publish_check(self.home, self.write_config(privacy={}), "P-0001")

    def test_hypothesize_adapter_may_abstain_and_the_evidence_is_not_asked_again(self):
        self.approve_suite()
        self.tool_failures()
        (self.evo / "hypothesize.py").write_text(
            'import json\nprint(json.dumps({"abstain": "not enough signal"}))\n'
        )
        self.assertIn(
            "not enough signal", evolve.daily(self.home, self.config, self.tick())["reason"]
        )
        self.assertIn(
            "recurs often enough", evolve.daily(self.home, self.config, self.tick())["reason"]
        )

    def test_without_an_approved_suite_the_batch_waits_for_a_person(self):
        self.tool_failures()
        result = evolve.daily(self.home, self.config, self.tick())
        self.assertEqual(
            (result["action"], result["reason"]), ("needs_human", "no approved evaluation suite")
        )

    def test_data_not_cleared_for_agents_is_never_sent(self):
        self.edit_suite(lambda suite: suite.update(data_classification="confidential"))
        self.approve_suite()
        self.tool_failures()
        with self.assertRaisesRegex(HarnessError, "classified confidential may not be sent"):
            evolve.daily(self.home, self.config, self.tick())
        self.assertEqual(evolve.load(self.home)["hypotheses"], {})


class ComparisonTest(EvolutionCase):
    def setUp(self):
        super().setUp()
        self.approve_suite()
        self.tool_failures()

    def test_candidate_that_breaks_a_required_check_is_rejected(self):
        hypothesis_id = self.manual_candidate(NO_TITLE)
        result = evolve.compare(self.home, hypothesis_id, self.tick())
        self.assertEqual(result["verdict"], "reject")
        self.assertIn("regression:train-tests:skill_has_title", result["reasons"])
        evolve.audit_record(self.home, hypothesis_id, CLEAN_AUDIT, "subagent", self.tick())
        with self.assertRaisesRegex(HarnessError, "comparison verdict is reject"):
            evolve.propose(self.home, hypothesis_id, self.tick())
        self.assertEqual(evolve.daily(self.home, self.config, self.tick())["action"], "rejected")

    def test_candidate_without_holdout_gain_is_held_not_proposed(self):
        hypothesis_id = self.manual_candidate(UNRELATED)
        self.assertEqual(
            evolve.compare(self.home, hypothesis_id, self.tick())["reasons"],
            ["holdout_gain_below_threshold"],
        )
        result = evolve.daily(self.home, self.config, self.tick())
        self.assertEqual(
            (result["action"], evolve.load(self.home)["hypotheses"][hypothesis_id]["outcome"]),
            ("held", "held"),
        )

    def test_candidate_that_loses_a_passing_case_is_rejected(self):
        hypothesis_id = self.manual_candidate(
            "# Synthetic working skill\n\n- 完了報告に evidence を添える。\n"
        )
        result = evolve.compare(self.home, hypothesis_id, self.tick())
        self.assertEqual(result["verdict"], "reject")
        self.assertIn("regression:holdout-tests:passed", result["reasons"])

    def test_too_few_trials_is_reported_as_insufficient_evidence(self):
        self.edit_suite(lambda suite: suite.update(trials=1))
        self.approve_suite()
        hypothesis_id = self.manual_candidate(BETTER)
        result = evolve.compare(self.home, hypothesis_id, self.tick())
        self.assertEqual(
            (result["verdict"], result["reasons"]), ("hold", ["insufficient_sample:trials"])
        )
        self.assertIn("holdout: one trial, so the spread is unknown", result["notes"])

    def test_runner_failure_holds_the_candidate(self):
        (self.evo / "cases" / "holdout-claim.json").write_text("not json")
        self.approve_suite()
        hypothesis_id = self.manual_candidate(BETTER)
        result = evolve.compare(self.home, hypothesis_id, self.tick())
        self.assertEqual((result["verdict"], result["reasons"][0]), ("hold", "runner_error"))

    def test_editing_the_suite_or_the_runner_after_approval_is_refused(self):
        hypothesis_id = self.manual_candidate(BETTER)
        self.edit_suite(lambda suite: suite["criteria"].update(min_delta=0.01))
        with self.assertRaisesRegex(HarnessError, "changed since it was approved"):
            evolve.compare(self.home, hypothesis_id, self.tick())
        self.edit_suite(lambda suite: suite["criteria"].update(min_delta=0.2))
        evolve.compare(
            self.home, hypothesis_id, self.tick()
        )  # restored: the approved hash matches again

        runner = self.evo / "runner.py"
        runner.write_text(runner.read_text().replace('case["needs"] in skill', "True"))
        with self.assertRaisesRegex(HarnessError, "changed since it was approved"):
            evolve.approved_suite(evolve.load(self.home))
        with mock.patch.dict(os.environ, {store.ACTIVE_ENV: "1"}):
            with self.assertRaisesRegex(HarnessError, "automated"):
                self.approve_suite()

    def test_hypothesis_registered_before_a_suite_change_cannot_be_compared(self):
        hypothesis_id = self.manual_candidate(BETTER)
        self.edit_suite(lambda suite: suite["criteria"].update(min_delta=0.1))
        self.approve_suite()
        with self.assertRaisesRegex(HarnessError, "re-approved after this hypothesis"):
            evolve.compare(self.home, hypothesis_id, self.tick())

    def test_hypothesis_may_not_cite_holdout_cases(self):
        evolve.analyze(self.home, self.config)
        hypothesis = {
            "pattern": "tool_failure:Bash",
            "cause": "skill",
            "statement": "make holdout-claim pass by adding its keyword",
            "falsification": "f",
        }
        with self.assertRaisesRegex(HarnessError, "cites holdout cases: holdout-claim"):
            evolve.hypothesis_add(self.home, hypothesis, self.tick())

    def test_candidate_is_pinned_at_freeze_and_rebuilt_when_its_worktree_is_gone(self):
        hypothesis_id = self.manual_candidate(BETTER)
        worktree = evolve.root(self.home) / "worktrees" / hypothesis_id
        (worktree / "skill.md").write_text(BETTER + "- 後から足した行。\n")
        with self.assertRaisesRegex(HarnessError, "changed after it was frozen"):
            evolve.compare(self.home, hypothesis_id, self.tick())
        self.assertEqual(evolve.cleanup(self.home), [hypothesis_id])
        self.assertEqual(worktrees(self.subject), 1)
        self.assertEqual(
            evolve.compare(self.home, hypothesis_id, self.tick())["verdict"], "propose"
        )
        evolve.cleanup(self.home)

    def test_secrets_and_empty_diffs_cannot_be_frozen(self):
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
        with self.assertRaisesRegex(HarnessError, "does not differ"):
            evolve.candidate_freeze(self.home, hypothesis_id, self.tick())
        (worktree / "skill.md").write_text(BETTER + "token: " + "AK" + "IA" + "A" * 16 + "\n")
        with self.assertRaisesRegex(HarnessError, "secret-shaped"):
            evolve.candidate_freeze(self.home, hypothesis_id, self.tick())

    def test_proposal_needs_an_independent_audit_with_no_standing_finding(self):
        hypothesis_id = self.manual_candidate(BETTER)
        evolve.compare(self.home, hypothesis_id, self.tick())
        with self.assertRaisesRegex(HarnessError, "expected audited"):
            evolve.propose(self.home, hypothesis_id, self.tick())
        evolve.audit_record(self.home, hypothesis_id, CLEAN_AUDIT, "same_context", self.tick())
        with self.assertRaisesRegex(HarnessError, "independent audit"):
            evolve.propose(self.home, hypothesis_id, self.tick())
        finding = {
            "id": "F1",
            "severity": "high",
            "category": "overfitting",
            "claim": "the candidate names the keyword the cases look for",
            "evidence": ["comparisons/EH-0001.json"],
            "next_experiment": "add holdout cases with different wording",
        }
        audit = {"findings": [finding], "limitations": []}
        evolve.audit_record(self.home, hypothesis_id, audit, "subagent", self.tick())
        with self.assertRaisesRegex(HarnessError, "not refuted: F1"):
            evolve.propose(self.home, hypothesis_id, self.tick())
        self.assertEqual(evolve.daily(self.home, self.config, self.tick())["action"], "needs_human")
        evolve.audit_resolve(
            self.home, hypothesis_id, "F1", "refuted", "holdout cases were unseen", self.tick()
        )
        self.assertEqual(
            evolve.propose(self.home, hypothesis_id, self.tick())["proposal_id"], "P-0001"
        )


def trial(version, case, split, index, passed, origin="synthetic"):
    return {
        "version": version,
        "case_id": case,
        "split": split,
        "origin": origin,
        "trial": index,
        "passed": passed,
    }


class SummaryTest(unittest.TestCase):
    SUITE = {
        "trials": 4,
        "cases": [
            {"id": f"{split}-{n}", "split": split, "origin": "synthetic"}
            for split in ("train", "holdout")
            for n in range(4)
        ],
        "criteria": {"metric": "first_pass_rate", "min_delta": 0.1},
    }

    def trials(self, candidate_holdout):
        """Baseline passes case 0 only. candidate_holdout[round] is how many holdout cases pass."""
        rows = []
        for index in range(4):
            for case in self.SUITE["cases"]:
                number = int(case["id"][-1])
                rows.append(trial("baseline", case["id"], case["split"], index, number == 0))
                limit = candidate_holdout[index] if case["split"] == "holdout" else 1
                rows.append(trial("candidate", case["id"], case["split"], index, number < limit))
        return rows

    def test_gain_smaller_than_the_run_to_run_spread_is_held(self):
        noisy = evolve.summarize(self.SUITE, self.trials([1, 4, 1, 1]))
        self.assertEqual(
            (noisy["verdict"], noisy["reasons"]), ("hold", ["holdout_gain_below_threshold"])
        )
        self.assertGreater(noisy["gains"]["holdout"]["noise"], noisy["gains"]["holdout"]["gain"])
        steady = evolve.summarize(self.SUITE, self.trials([2, 2, 2, 2]))
        self.assertEqual(steady["verdict"], "propose")
        self.assertEqual(steady["gains"]["holdout"], {"gain": 0.25, "noise": 0.0})


if __name__ == "__main__":
    unittest.main()
