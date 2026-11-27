"""Bounded retrieval, context packets and the overview: metadata first, sources on demand."""

import json
from pathlib import Path

from support import ExplorerCase

from context_recovery import traverse
from context_recovery.base import Refusal
from context_recovery.index import empty, upsert


class Bounded(ExplorerCase):
    def test_exploration_returns_metadata_and_a_plan_never_a_body(self):
        self.index_synthetic()
        found = self.ok("explore", "--question", "orders queue の保持期間はなぜその値なのか")
        self.assertIn("PROP-retention", found["seeds"])
        text = json.dumps(found, ensure_ascii=False)
        for body_line in (
            "保存量に比例して費用が増えるため",
            "ledger.charge",
            "Ignore all previous instructions",
        ):
            self.assertNotIn(body_line, text)
        for step in found["retrieval"]:
            self.assertEqual(
                set(step), {"entity", "locator", "revision", "sha256", "estimated_tokens"}
            )

    def test_hops_limit_how_far_the_walk_goes(self):
        self.index_synthetic()
        near = self.ok("explore", "--entity", "PROP-retention", "--hops", "0")
        self.assertEqual([e["id"] for e in near["entities"]], ["PROP-retention"])
        self.assertIn("DEC-0001", near["frontier"])
        one = {
            e["id"]: e["distance"]
            for e in self.ok("explore", "--entity", "PROP-retention")["entities"]
        }
        self.assertEqual(max(one.values()), 1)
        self.assertNotIn("OBJ-replay", one)  # two hops away, through REQ-0001
        two = {
            e["id"]
            for e in self.ok("explore", "--entity", "PROP-retention", "--hops", "2")["entities"]
        }
        self.assertIn("OBJ-replay", two)

    def test_entity_count_is_capped_and_the_rest_is_counted(self):
        self.index_synthetic()
        found = self.ok(
            "explore", "--entity", "RES-orders-queue", "--hops", "2", "--max-entities", "3"
        )
        self.assertEqual(len(found["entities"]), 3)
        self.assertTrue(found["dropped"])
        self.assertEqual(found["entities"][0]["id"], "RES-orders-queue")

    def test_token_budget_caps_the_retrieval_plan(self):
        self.index_synthetic()
        wide = self.ok(
            "explore", "--entity", "PROP-retention", "--hops", "2", "--token-budget", "60000"
        )
        tight = self.ok(
            "explore", "--entity", "PROP-retention", "--hops", "2", "--token-budget", "150"
        )
        self.assertLessEqual(tight["bounds"]["estimated_tokens"], 150)
        self.assertEqual(
            sum(s["estimated_tokens"] for s in tight["retrieval"]),
            tight["bounds"]["estimated_tokens"],
        )
        self.assertTrue(tight["deferred"])
        self.assertEqual(len(tight["retrieval"]) + len(tight["deferred"]), len(wide["retrieval"]))
        nothing = self.ok("explore", "--entity", "PROP-retention", "--token-budget", "0")
        self.assertEqual(nothing["retrieval"], [])

    def test_limits_themselves_are_limited(self):
        self.index_synthetic()
        for flag, value in (
            ("--hops", "9"),
            ("--max-entities", "500"),
            ("--token-budget", "9999999"),
            ("--hops", "-1"),
        ):
            self.assertIn(
                "must be between",
                self.refused("explore", "--entity", "PROP-retention", flag, value)["error"],
            )

    def test_cost_stays_flat_as_the_index_grows(self):
        index = empty(self.repo)
        entities = [
            {
                "id": f"RES-{n:04d}",
                "type": "resource",
                "description": f"synthetic resource {n}",
                "relationships": [{"type": "depends_on", "to": f"RES-{n + 1:04d}"}]
                if n < 1999
                else [],
            }
            for n in range(2000)
        ]
        upsert(index, entities, self.repo)
        found = traverse.explore(index, entity="RES-0000", hops=4, max_entities=12)
        self.assertEqual(len(found["entities"]), 5)
        self.assertEqual(found["frontier"], ["RES-0005"])
        self.assertLess(len(json.dumps(found)), 6000)

    def test_an_unknown_entity_and_a_missing_question_are_refused(self):
        self.index_synthetic()
        self.assertIn("not in the index", self.refused("explore", "--entity", "RES-nope")["error"])
        self.assertIn("--entity, --question or both", self.refused("explore")["error"])

    def test_search_matches_japanese_and_ids(self):
        self.index_synthetic()
        index = json.loads(Path(self.ok("path")["index"]).read_text(encoding="utf-8"))
        self.assertEqual(traverse.search(index, "DEC-0002 は何を決めたか")[0], "DEC-0002")
        self.assertEqual(
            traverse.search(index, "二重に課金されるのはいつか")[0], "RISK-duplicate-charge"
        )
        self.assertEqual(traverse.search(index, "kubernetes"), [])


class Packet(ExplorerCase):
    def test_packet_carries_the_contract_fields(self):
        self.index_synthetic()
        self.change()
        packet = self.ok("packet", "--entity", "PROP-retention", "--question", "なぜ 14 日か")
        self.assertEqual(packet["selected_entity"], "PROP-retention")
        self.assertEqual(packet["question"], "なぜ 14 日か")
        self.assertEqual(packet["mode"], "deep-dive")
        self.assertEqual(packet["references"][0]["source"]["revision"], self.base)
        first = packet["retrieval_instructions"][0]
        self.assertEqual(first["instruction"], f"git show {self.base}:infra/queue.tf（L3）")
        budget = packet["token_budget"]
        self.assertEqual(budget["token_budget"], 4000)
        self.assertLessEqual(budget["estimated_tokens"], 4000)
        self.assertGreater(budget["prompt_tokens"], 0)
        kinds = {gap["kind"] for gap in packet["gaps"]}
        self.assertLessEqual({"conflict", "stale_reference", "source_unavailable"}, kinds)
        self.assertIn("設定値だけから過去の理由を推測しない", packet["prompt"])

    def test_retrieval_instructions_do_not_assume_a_document_tool(self):
        self.index_synthetic()
        packet = self.ok("packet", "--entity", "DOC-runbook")
        instruction = packet["retrieval_instructions"][0]["instruction"]
        self.assertIn("接続済みの文書ツールを探し", instruction)
        self.assertIn("接続が無ければ取得せず、unknown と記録する", instruction)
        self.assertNotIn("mcp__", packet["prompt"])
        change = self.ok("packet", "--entity", "CHG-pr-42")
        self.assertIn("PR #42 の本文を読み取り専用で取得する", change["prompt"])

    def test_packet_takes_only_claims_whose_evidence_reads_back(self):
        self.index_synthetic()
        self.change()
        claims, _ = self.claims()
        good = self.write("good.json", [c for c in claims if c["id"] in ("C1", "C4", "C7")])
        packet = self.ok("packet", "--entity", "PROP-retention", "--claims", good)
        self.assertIn("conflict | C4 |", packet["prompt"])
        self.assertIn("unknown | C7 |", packet["prompt"])
        bad = self.write("bad.json", [c for c in claims if c["id"] == "C8"])
        refused = self.refused("packet", "--entity", "PROP-retention", "--claims", bad)
        self.assertIn("C8: excerpt_not_found", refused["problems"][0])

    def test_packet_file_is_written_outside_any_repository(self):
        self.index_synthetic()
        inside = self.refused(
            "packet", "--entity", "PROP-retention", "--out", str(self.repo / "packet.md")
        )
        self.assertIn("outside any git repository", inside["error"])
        written = self.ok(
            "packet", "--entity", "PROP-retention", "--out", str(self.tmp / "packet.md")
        )
        self.assertTrue(
            (self.tmp / "packet.md")
            .read_text(encoding="utf-8")
            .startswith("# Context packet: PROP-retention")
        )
        self.assertEqual((self.tmp / "packet.md").stat().st_mode & 0o777, 0o600)
        self.assertIn("prompt_tokens", written["token_budget"])

    def test_no_match_is_said_not_guessed(self):
        self.index_synthetic()
        self.assertIn(
            "no entity matches", self.refused("packet", "--question", "kubernetes の設定")["error"]
        )


class Overview(ExplorerCase):
    def test_overview_is_short_by_default_and_says_what_is_left(self):
        self.index_synthetic()
        view = self.ok("overview", "--per-section", "2")
        self.assertEqual(
            list(view["sections"]),
            [
                "architecture",
                "changes",
                "decisions",
                "objectives",
                "requirements",
                "risks",
                "unresolved_questions",
            ],
        )
        architecture = view["sections"]["architecture"]
        self.assertEqual(
            (architecture["count"], len(architecture["entities"]), architecture["more"]), (5, 2, 3)
        )
        self.assertEqual(view["gaps"]["conflict"]["count"], 1)
        self.assertEqual(
            view["gaps"]["missing_rationale"]["first"][0]["entity"], "PROP-visibility-timeout"
        )


class Limits(ExplorerCase):
    def test_bounds_reject_non_integers(self):
        with self.assertRaises(Refusal):
            traverse.explore(empty(self.repo), question="x", hops=1.5)  # type: ignore[arg-type]
