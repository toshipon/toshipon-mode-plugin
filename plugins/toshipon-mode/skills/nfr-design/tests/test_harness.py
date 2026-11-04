import copy
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "runtime"))
from harness.evaluator import evaluate  # noqa: E402
from harness.runner import claude_backend, demo_backend, demo_design, evaluate_candidate, run_loop  # noqa: E402
from harness.workspace import WorkspaceError, load_workspace  # noqa: E402

SCRIPT = SKILL / "scripts/nfr-harness.py"
DOC_CHECK = SKILL.parent / "toshipon-mode/scripts/doc-check.sh"


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nfr harness ")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "workspace with spaces"
        shutil.copytree(SKILL / "examples/workspace", self.ws)
        self.data = json.loads((self.ws / "input.json").read_text())
        self.review = {"findings": [], "limitations": []}

    def test_valid_fixture(self):
        self.assertTrue(evaluate(self.data, demo_design(self.data), self.review)["passed"])

    def test_each_mutation_blocks_gate(self):
        mutations = [
            lambda d: d["requirements"][0].update(target=90),
            lambda d: d["decisions"].pop(),
            lambda d: d["verifications"].pop(),
            lambda d: d["verifications"][0].update(unit="seconds"),
            lambda d: d["verifications"][0].update(target=0),
            lambda d: d["decisions"][0].update(requirement_ids=["REQ-999"]),
            lambda d: d["decisions"][0].update(evidence_ids=["E999"]),
            lambda d: d["verifications"][0].update(requirement_id="REQ-999"),
            lambda d: d["decisions"].append(copy.deepcopy(d["decisions"][0])),
            lambda d: d["aws"].update(rto_minutes=61),
            lambda d: d["aws"].update(rpo_minutes=16),
            lambda d: d["aws"].update(monthly_cost_usd=1501),
            lambda d: d["aws"].update(az_count=1),
            lambda d: d["evidence"][0].update(status="unverified"),
            lambda d: d["evidence"][0].update(verified_by=""),
            lambda d: d["evidence"][0].update(url="https://docs.aws.amazon.com.evil.test/a"),
            lambda d: d["evidence"][0].update(claim="Fabricated verified claim"),
            lambda d: d["open_issues"].append(
                {
                    "id": "I1",
                    "severity": "high",
                    "status": "open",
                    "description": "missing approval",
                }
            ),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                design = demo_design(self.data)
                mutate(design)
                self.assertFalse(evaluate(self.data, design, self.review)["passed"])

    def test_unknown_metric_and_wrong_unit_fail(self):
        for metric, unit in [("invented", "minutes"), ("rto_minutes", "hours")]:
            with self.subTest(metric=metric, unit=unit):
                data = copy.deepcopy(self.data)
                data["requirements"][2].update(metric=metric, unit=unit)
                self.assertFalse(evaluate(data, demo_design(self.data), self.review)["passed"])

    def test_auditor_vetoes(self):
        for severity in ["critical", "high"]:
            review = {
                "findings": [
                    {"id": "A1", "severity": severity, "description": "unproven recovery"}
                ],
                "limitations": [],
            }
            self.assertFalse(evaluate(self.data, demo_design(self.data), review)["passed"])

    def test_schema_and_missing_audit_fail_closed(self):
        self.assertFalse(evaluate(self.data, {}, self.review)["passed"])
        self.assertFalse(evaluate(self.data, demo_design(self.data), {})["passed"])

    def test_workspace_unknown_source_rejected(self):
        self.data["requirements"][0]["source_refs"] = ["S99"]
        result = load_workspace(self.ws, self.data)
        self.assertTrue(result["findings"])

    def test_technical_evidence_needs_source_ledger(self):
        self.data["technical_evidence"][0]["url"] = "https://docs.aws.amazon.com/unknown"
        result = load_workspace(self.ws, self.data)
        self.assertTrue(any(f["id"] == "WORKSPACE_EVIDENCE" for f in result["findings"]))

    def test_workspace_missing_ledger_rejected(self):
        (self.ws / "facts.md").unlink()
        with self.assertRaises(WorkspaceError):
            load_workspace(self.ws, self.data)

    def test_workspace_questions_block_without_model_approval(self):
        for state in ["open", "asked", "assumed"]:
            with self.subTest(state=state):
                with (self.ws / "questions.tsv").open("w", newline="") as f:
                    w = csv.writer(f, delimiter="\t", lineterminator="\n")
                    w.writerow(
                        "id question blocks owner status asked_at answer evidence incorporated_in updated".split()
                    )
                    w.writerow(
                        [
                            "Q1",
                            "承認を要する",
                            "REQ-001",
                            "業務責任者",
                            state,
                            "",
                            "仮定",
                            "",
                            "",
                            "2026-11-03",
                        ]
                    )
                self.assertFalse(
                    run_loop(
                        self.data,
                        self.ws / ("blocked-" + state),
                        demo_backend,
                        2,
                        workspace=self.ws,
                    )
                )

    def test_demo_repairs_and_doc_check_passes(self):
        out = self.ws / "demo"
        self.assertTrue(run_loop(self.data, out, demo_backend, 3, workspace=self.ws))
        self.assertFalse(json.loads((out / "iteration-01/evaluation.json").read_text())["passed"])
        self.assertTrue(json.loads((out / "iteration-02/evaluation.json").read_text())["passed"])
        for name in [
            "requirements.md",
            "design.md",
            "verification.md",
            "input.json",
            "metadata.json",
            "sources.tsv",
            "questions.tsv",
        ]:
            self.assertTrue((out / name).is_file(), name)
        check = subprocess.run(
            ["/bin/bash", str(DOC_CHECK), str(out)], capture_output=True, text=True
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("0 errors", check.stdout)
        with self.assertRaises(ValueError):
            run_loop(self.data, out, demo_backend, 3, workspace=self.ws)

    def test_output_must_be_new_direct_child(self):
        for out in [self.ws, self.ws.parent / "outside", self.ws / "nested" / "run"]:
            with self.subTest(out=out), self.assertRaises(ValueError):
                run_loop(self.data, out, demo_backend, 2, workspace=self.ws)

    def test_backend_failure_and_iteration_limit_fail_closed(self):
        def invalid(role, payload, schema):
            return {}

        def failing(role, payload, schema):
            raise RuntimeError("simulated service failure")

        for backend in [invalid, failing]:
            with self.subTest(backend=backend.__name__):
                out = self.ws / backend.__name__
                self.assertFalse(run_loop(self.data, out, backend, 2, workspace=self.ws))
                self.assertFalse((out / "design.md").exists())
                self.assertFalse(json.loads((out / "evaluation.json").read_text())["passed"])

    def test_changed_ledger_aborts(self):
        def changing(role, payload, schema):
            if role == "designer":
                with (self.ws / "facts.md").open("a") as f:
                    f.write("changed upstream\n")
            return demo_backend(role, payload, schema)

        self.assertFalse(run_loop(self.data, self.ws / "changed", changing, 2, workspace=self.ws))

    def test_cli_protocol_rejects_errors(self):
        for stdout in [
            "{}",
            "[]",
            '{"is_error":true}',
            '{"subtype":"error_max_turns"}',
            "not json",
        ]:
            with (
                self.subTest(stdout=stdout),
                patch(
                    "harness.runner.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, stdout, ""),
                ),
            ):
                with self.assertRaises((ValueError, RuntimeError)):
                    claude_backend("designer", {}, {})

    def test_cli_protocol_disables_tools(self):
        with patch(
            "harness.runner.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, '{"subtype":"success","structured_output":{"ok":true}}', ""
            ),
        ) as call:
            self.assertEqual(
                claude_backend(
                    "designer",
                    {},
                    {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"},
                ),
                {"ok": True},
            )
            args = call.call_args.args[0]
            self.assertIn("--bare", args)
            self.assertNotIn("$schema", json.loads(args[args.index("--json-schema") + 1]))
            self.assertEqual(args[args.index("--tools") + 1], "")
            self.assertIn("--strict-mcp-config", args)
            self.assertIn("--disable-slash-commands", args)
            self.assertNotIn("shell", call.call_args.kwargs)

    def test_evaluate_timeout_cannot_leave_pass(self):
        out = self.ws / "timeout"
        with patch(
            "harness.runner.subprocess.run", side_effect=subprocess.TimeoutExpired("doc-check", 30)
        ):
            self.assertFalse(
                evaluate_candidate(self.data, demo_design(self.data), self.review, self.ws, out)
            )
        self.assertFalse(json.loads((out / "evaluation.json").read_text())["passed"])
        self.assertTrue((out / "metadata.json").is_file())
        self.assertFalse((out / "verification.md").exists())

    def test_evaluate_candidate_snapshots_metadata(self):
        out = self.ws / "evaluated"
        self.assertTrue(
            evaluate_candidate(self.data, demo_design(self.data), self.review, self.ws, out)
        )
        metadata = json.loads((out / "metadata.json").read_text())
        self.assertIn("input_sha256", metadata)
        self.assertIn("workspace_sha256", metadata)

    def test_invalid_evidence_registry_not_just_candidate_is_rejected(self):
        for key, value in [
            ("status", "unverified"),
            ("verified_by", " "),
            ("checked_at", "2026-02-30"),
            ("url", "https://aws.amazon.com@evil.test/a"),
        ]:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data["technical_evidence"][0][key] = value
                self.assertFalse(evaluate(data, demo_design(data), self.review)["passed"])

    def test_empty_source_locator_rejected(self):
        (self.ws / "sources.tsv").write_text(
            "id\tkind\tlocator\ttitle\tretrieved\tnote\nS1\tother\t\tfixture\t2026-11-03\tfixture\n"
        )
        with self.assertRaises(WorkspaceError):
            load_workspace(self.ws, self.data)

    def test_answered_question_requires_evidence(self):
        with (self.ws / "questions.tsv").open("a") as f:
            f.write("Q1\t承認はあるか\tREQ-001\towner\tanswered\t\t\t\t\t2026-11-03\n")
        with self.assertRaises(WorkspaceError):
            load_workspace(self.ws, self.data)

    def test_nonfinite_targets_fail(self):
        data = copy.deepcopy(self.data)
        data["requirements"][0]["target"] = float("nan")
        self.assertFalse(evaluate(data, demo_design(data), self.review)["passed"])

    def test_changed_input_aborts(self):
        def changing(role, payload, schema):
            if role == "designer":
                data = copy.deepcopy(self.data)
                data["requirements"][0]["target"] = 90
                (self.ws / "input.json").write_text(json.dumps(data))
            return demo_backend(role, payload, schema)

        self.assertFalse(
            run_loop(
                self.data,
                self.ws / "changed-input",
                changing,
                2,
                workspace=self.ws,
                input_path=self.ws / "input.json",
            )
        )

    def test_cli_exit_codes_and_evaluate_output(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from harness.__main__ import main

        candidate = self.ws / "candidate.json"
        audit = self.ws / "audit.json"
        candidate.write_text(json.dumps(demo_design(self.data)))
        audit.write_text(json.dumps(self.review))
        args = [
            "nfr-harness",
            "evaluate",
            "--input",
            str(self.ws / "input.json"),
            "--workspace",
            str(self.ws),
            "--design",
            str(candidate),
            "--review",
            str(audit),
            "--out",
            str(self.ws / "cli-evaluate"),
        ]
        with patch.object(sys, "argv", args), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertTrue((self.ws / "cli-evaluate/metadata.json").is_file())
        args[-1] = str(self.ws / "cli-invalid")
        candidate.write_text("{}")
        with patch.object(sys, "argv", args), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        args[args.index("--input") + 1] = str(self.ws / "missing.json")
        with patch.object(sys, "argv", args), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 2)

    def test_doc_check_rejection_does_not_publish(self):
        out = self.ws / "doc-check-failed"
        with patch(
            "harness.runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "ERROR invalid document", ""),
        ):
            self.assertFalse(
                evaluate_candidate(self.data, demo_design(self.data), self.review, self.ws, out)
            )
        self.assertFalse((out / "requirements.md").exists())
        self.assertFalse(json.loads((out / "evaluation.json").read_text())["passed"])

    def test_ledger_changes_during_doc_check_prevent_publish(self):
        out = self.ws / "changed-during-check"

        def changing(*args, **kwargs):
            (self.ws / "facts.md").write_text("changed after checking")
            return subprocess.CompletedProcess([], 0, "doc-check: 0 errors, 0 warnings", "")

        with patch("harness.runner.subprocess.run", side_effect=changing):
            self.assertFalse(
                evaluate_candidate(self.data, demo_design(self.data), self.review, self.ws, out)
            )
        self.assertFalse((out / "requirements.md").exists())

    def test_script_from_unrelated_cwd(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "demo",
                "--input",
                str(self.ws / "input.json"),
                "--workspace",
                str(self.ws),
                "--out",
                str(self.ws / "cli"),
            ],
            cwd=self.tmp.name,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
