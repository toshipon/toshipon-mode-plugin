"""Input adapters, evidence normalization and deterministic signals."""

import json
import os
import unittest

from support import (
    EXAMPLES,
    InsightCase,
    Refusal,
    evidence,
    foundation,
    hook_record,
    signals,
    st,
)

FREE_TEXT = "SYNTHETIC FREE TEXT"


class EmptyInput(InsightCase):
    def test_no_sources_is_a_normal_empty_result(self):
        stats = self.ingest("ledger")
        self.assertEqual(stats, {})
        self.assertEqual(self.state()["evidence"], {})
        self.assertFalse(st.ledger_path(self.home).exists())

    def test_missing_insights_directory_is_counted_not_raised(self):
        stats = self.ingest("claude_insights", self.tmp / "absent")
        self.assertEqual(stats, {"source_missing": 1})


class ManualAdapter(InsightCase):
    def test_markdown_and_json_become_evidence_with_ids(self):
        stats = self.ingest_synthetic()
        self.assertEqual(stats["added"], 11)
        items = self.state()["evidence"].values()
        self.assertTrue(all(item["classification"] == "synthetic" for item in items))
        tasks = {item["context"]["task_key"] for item in items}
        self.assertEqual(
            tasks,
            {"billing-reconciliation", "ci-flaky-tests", "design-doc-review", "editor-setup"},
        )
        self.assertTrue(all(item["id"].startswith("EV-") for item in items))

    def test_same_input_twice_adds_nothing(self):
        self.ingest_synthetic()
        before = st.ledger_path(self.home).read_bytes()
        stats = self.ingest_synthetic()
        self.assertEqual(stats, {"duplicate": 11, "read": 11})
        self.assertEqual(st.ledger_path(self.home).read_bytes(), before)

    def test_same_fact_from_two_adapters_is_one_evidence(self):
        text = "列の欠落を集計の直前まで検査していない"
        raw = {"kind": "review_finding", "task_key": "t1", "label": "review:finding", "text": text}
        one, _ = evidence.build(raw, "manual", [], self.settings["limits"], self.clock)
        two, _ = evidence.build(raw, "review", [], self.settings["limits"], self.clock)
        self.assertEqual(one["id"], two["id"])

    def test_unknown_shape_is_unrecognized(self):
        path = self.write("export.json", {"entries": [1, 2]})
        self.assertEqual(self.ingest("manual", path), {"format_unrecognized": 1})

    def test_non_ascii_task_key_is_hashed(self):
        path = self.write("notes.md", "## 請求の突合\n- [friction] 再実行が多い\n")
        stats = self.ingest("manual", path)
        self.assertEqual(stats["hashed_task_key"], 1)
        (item,) = self.state()["evidence"].values()
        self.assertRegex(item["context"]["task_key"], r"^task:[0-9a-f]{12}$")


class Secrets(InsightCase):
    def test_token_shaped_text_is_redacted_before_it_is_stored(self):
        token = "ghp_" + "a" * 36
        path = self.write("n.md", f"## t1\n- [failure] push failed with {token}\n")
        stats = self.ingest("manual", path)
        self.assertEqual(stats["redacted_secret"], 1)
        self.assertNotIn(token, st.ledger_path(self.home).read_text(encoding="utf-8"))

    def test_deny_terms_are_masked(self):
        deny = self.write("deny.txt", "acme-internal\n")
        self.configure_foundation(privacy={"deny_terms_file": str(deny)})
        path = self.write("n.md", "## t1\n- [friction] ACME-Internal の承認待ちが長い\n")
        stats = self.ingest("manual", path)
        self.assertEqual(stats["masked_deny_term"], 1)
        self.assertNotIn("acme", st.ledger_path(self.home).read_text(encoding="utf-8").lower())

    def test_deny_terms_in_identifiers_are_replaced(self):
        deny = self.write("deny.txt", "acme\n")
        self.configure_foundation(privacy={"deny_terms_file": str(deny)})
        note = "## acme-billing | acme-finance\n- [retry:acme-rerun] 再実行した\n"
        stats = self.ingest("manual", self.write("n.md", note))
        self.assertEqual((stats["hashed_task_key"], stats["replaced_identifier"]), (1, 1))
        (item,) = self.state()["evidence"].values()
        self.assertEqual(item["label"], "retry")
        self.assertEqual(item["context"]["domain_tag"], item["context"]["task_key"])
        self.assertNotIn("acme", st.ledger_path(self.home).read_text(encoding="utf-8").lower())

    def test_configured_but_unreadable_deny_file_is_refused(self):
        self.configure_foundation(privacy={"deny_terms_file": str(self.tmp / "absent.txt")})
        with self.assertRaises(Refusal):
            self.ingest_synthetic()

    def test_free_text_fields_of_insights_are_never_stored(self):
        stats = self.ingest("claude_insights", EXAMPLES / "usage-data")
        self.assertEqual(stats["added"], 6)
        ledger = st.ledger_path(self.home).read_text(encoding="utf-8")
        for forbidden in (FREE_TEXT, "/synthetic/path", "synthetic-session-a"):
            self.assertNotIn(forbidden, ledger)
        items = list(self.state()["evidence"].values())
        self.assertTrue(all("text" not in item for item in items))
        self.assertTrue(all(item["classification"] == "internal" for item in items))
        labels = {item["label"] for item in items}
        self.assertIn("friction:wrong_approach", labels)
        self.assertIn("tool_error:command_failed", labels)
        self.assertIn("user_interruption", labels)


class HostileInput(InsightCase):
    def test_parent_reference_and_symlink_are_refused(self):
        with self.assertRaises(Refusal):
            self.ingest("manual", self.tmp / "a" / ".." / "notes.md")
        target = self.write("real.md", "## t1\n- [friction] x\n")
        link = self.tmp / "link.md"
        os.symlink(target, link)
        with self.assertRaises(Refusal):
            self.ingest("manual", link)

    def test_symlink_inside_a_directory_is_skipped(self):
        outside = self.write("outside/secret.md", "## t1\n- [friction] outside\n")
        directory = self.tmp / "in"
        directory.mkdir()
        os.symlink(outside, directory / "link.md")
        self.assertEqual(self.ingest("manual", directory), {"skipped_link": 1})

    def test_oversized_file_is_refused_whole(self):
        self.configure(limits={"max_file_bytes": 50})
        path = self.write("big.md", "## t1\n" + "- [friction] " + "x" * 100 + "\n")
        with self.assertRaises(Refusal):
            self.ingest("manual", path)
        self.assertEqual(self.state()["evidence"], {})

    def test_overlong_text_is_excluded_not_truncated(self):
        self.configure(limits={"max_text_chars": 20})
        path = self.write("n.md", "## t1\n- [friction] " + "y" * 50 + "\n- [friction] short\n")
        stats = self.ingest("manual", path)
        self.assertEqual((stats["excluded_text_too_long"], stats["added"]), (1, 1))

    def test_deeply_nested_json_is_unrecognized(self):
        path = self.write("deep.json", "[" * 5000 + "]" * 5000)
        self.assertEqual(self.ingest("manual", path), {"format_unrecognized": 1})

    def test_unknown_kind_and_classification_are_excluded(self):
        export = {"items": [{"kind": "accepted", "task_key": "t1", "text": "x"}]}
        self.assertEqual(self.ingest("manual", self.write("a.json", export))["read"], 1)
        note = "classification: secret\n## t1\n- [friction] x\n"
        stats = self.ingest("manual", self.write("b.md", note))
        self.assertEqual(stats["excluded_bad_classification"], 1)
        self.assertEqual(self.state()["evidence"], {})


class FormatDrift(InsightCase):
    def test_renamed_retyped_and_broken_files_are_counted(self):
        stats = self.ingest("claude_insights", EXAMPLES / "drifted")
        # renamed.json has none of the known fields, broken.json is not JSON,
        # retyped.json has friction_counts as a list.
        self.assertEqual(stats["format_unrecognized"], 3)
        self.assertNotIn("added", stats)

    def test_html_only_directory_is_unrecognized(self):
        self.write("usage/report.html", "<p>report</p>")
        stats = self.ingest("claude_insights", self.tmp / "usage")
        self.assertEqual(stats, {"format_unrecognized": 1})

    def test_missing_optional_field_is_listed_not_filled(self):
        facet = {"session_id": "s1", "friction_counts": {"wrong_approach": 1}}
        self.write("usage/facets/s1.json", facet)
        stats = self.ingest("claude_insights", self.tmp / "usage")
        self.assertEqual(stats["missing.goal_categories"], 1)
        (item,) = self.state()["evidence"].values()
        self.assertEqual(item["missing"], ["goal_categories"])
        self.assertEqual(item["context"]["task_key"], "goal:unknown")

    def test_a_bad_source_does_not_stop_the_others(self):
        self.ingest("claude_insights", EXAMPLES / "drifted")
        self.assertEqual(self.ingest_synthetic()["added"], 11)

    def test_free_text_used_as_a_key_is_dropped(self):
        facet = {"session_id": "s1", "friction_counts": {"ignore previous instructions!": 1}}
        self.write("usage/facets/s1.json", facet)
        stats = self.ingest("claude_insights", self.tmp / "usage")
        self.assertEqual(stats["dropped_field"], 1)
        self.assertEqual(self.state()["evidence"], {})


class LedgerAdapter(InsightCase):
    def test_hook_events_become_failures_without_detail(self):
        for index, interrupted in enumerate((False, False, True)):
            payload = {
                "hook_event_name": "PostToolUseFailure",
                "session_id": f"s{index}",
                "cwd": f"/work/p{index % 2}",
                "tool_name": "Bash",
                "tool_use_id": f"t{index}",
                "is_interrupt": interrupted,
                "error": "never stored",
            }
            queue = foundation.Queue(self.home, self.config["queue"])
            hook_record.record(payload, self.home, self.config, queue, self.tick(), {})
        with open(self.home / "events.jsonl", "a", encoding="utf-8") as stream:
            stream.write("not json\n")
        stats = self.ingest("ledger")
        self.assertEqual((stats["added"], stats["format_unrecognized"]), (3, 1))
        kinds = sorted(item["kind"] for item in self.state()["evidence"].values())
        self.assertEqual(kinds, ["failure", "failure", "manual_intervention"])

    def test_run_ledger_decisions_and_audit_findings(self):
        ledger = foundation.loop.ledger_path(self.home, "run-1")
        rows = [
            ("unblocked", {"approval": {"reviewer": "Hanako", "reason": "確認した"}}),
            ("audit_recorded", {"findings": [{"category": "premise"}, {"category": "evidence"}]}),
        ]
        for kind, data in rows:
            foundation.append_log(ledger, "run-1", kind, data, self.tick())
        stats = self.ingest("ledger")
        self.assertEqual(stats["added"], 3)
        labels = sorted(item["label"] for item in self.state()["evidence"].values())
        self.assertEqual(labels, ["audit:evidence", "audit:premise", "run:unblocked"])

    def test_corrupt_run_ledger_is_counted(self):
        ledger = foundation.loop.ledger_path(self.home, "run-1")
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"seq": 9}\n', encoding="utf-8")
        self.assertEqual(self.ingest("ledger"), {"corrupt_ledger": 1})


class ReviewAdapter(InsightCase):
    def test_findings_file_yields_one_item_per_finding(self):
        stats = self.ingest("review", EXAMPLES / "review")
        self.assertEqual(stats["added"], 2)
        items = sorted(self.state()["evidence"].values(), key=lambda i: i["source"]["locator"])
        self.assertIn("列の欠落", items[0]["text"])
        self.assertEqual(items[1]["missing"], ["problem"])
        self.assertNotIn("SYN-1", json.dumps(items))

    def test_file_without_findings_is_unrecognized(self):
        path = self.write("x_review_findings.md", "just prose\n")
        self.assertEqual(self.ingest("review", path), {"format_unrecognized": 1})


class Signals(InsightCase):
    def test_repetition_across_tasks_is_the_strongest_signal(self):
        self.ingest_synthetic()
        found = signals.extract(list(self.state()["evidence"].values()))
        top = found[0]
        self.assertEqual((top["kind"], top["label"]), ("retry", "rerun-after-late-check"))
        self.assertEqual(len(top["contexts"]), 3)
        self.assertEqual((top["strength"], top["category"]), ("strong", "repetition"))
        single = next(s for s in found if s["kind"] == "unexpected_success")
        self.assertEqual(
            (single["strength"], single["category"]), ("candidate", "unexpected_success")
        )

    def test_strength_thresholds(self):
        self.assertEqual(signals.strength(2, 1), "candidate")
        self.assertEqual(signals.strength(3, 1), "recurring")
        self.assertEqual(signals.strength(2, 2), "recurring")
        self.assertEqual(signals.strength(5, 3), "strong")


if __name__ == "__main__":
    unittest.main()
