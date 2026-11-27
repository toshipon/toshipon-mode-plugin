"""The synthetic measurement and the launcher run end to end."""

import json
import subprocess
import sys

from support import EXAMPLES, ROOT, ExplorerCase


class Measure(ExplorerCase):
    def test_measure_reports_synthetic_numbers_and_says_what_it_did_not_measure(self):
        done = subprocess.run(
            [sys.executable, str(EXAMPLES / "measure.py")],
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(done.stdout)
        self.assertIn("synthetic", report["data"])
        self.assertEqual(report["time_to_understanding"], "not measured")
        checked = report["claim_check"]
        self.assertEqual(
            (checked["seeded_unsupported"], checked["seeded_unsupported_caught"]), (4, 4)
        )
        self.assertEqual(retrieval_bounds(report), {"hops": 1, "max_entities": 12})
        self.assertEqual(checked["supported_wrongly_rejected"], 0)
        retrieval = report["retrieval"]
        self.assertEqual(retrieval["questions"], 4)
        for row in retrieval["per_question"]:
            self.assertLessEqual(row["retrieved"], retrieval["bounds"]["max_entities"])
        self.assertGreater(retrieval["micro_recall"], 0.8)

    def test_the_launcher_runs_without_installing_anything(self):
        launcher = str(ROOT / "scripts" / "context-explorer.py")
        done = subprocess.run(
            [sys.executable, launcher, "--root", str(self.repo), "path"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(json.loads(done.stdout)["index"].endswith("index.json"))
        refused = subprocess.run(
            [sys.executable, launcher, "--root", str(self.repo), "explore"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--entity, --question or both", json.loads(refused.stderr)["error"])
        module = subprocess.run(
            [sys.executable, "-m", "context_recovery", "--root", str(self.repo), "check"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT / "runtime",
        )
        self.assertEqual(json.loads(module.stdout)["findings"], [])


def retrieval_bounds(report):
    return report["retrieval"]["bounds"]
