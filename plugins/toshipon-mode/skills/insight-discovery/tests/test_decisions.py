"""Human gates: the person's own hypothesis first, one selection a week, approved searches, handoffs."""

import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from support import (
    Backend,
    HarnessError,
    LoopCase,
    Refusal,
    decisions,
    foundation,
    pipeline,
    report,
    st,
)


class Decided(LoopCase):
    def setUp(self):
        super().setUp()
        self.run_daily()
        self.first, self.second = sorted(self.candidates())

    def render(self):
        return report.render(self.state(), self.settings, self.tick())

    def select(self, name, reviewer="Hanako"):
        return decisions.select(
            self.home, self.settings, name, reviewer, "今週試す価値がある", self.tick()
        )


class HumanPrior(Decided):
    def test_generated_hypotheses_stay_hidden_until_the_person_writes_theirs(self):
        text = self.render()
        self.assertIn("観測事実", text)
        self.assertIn("抽象化した問題", text)
        self.assertNotIn("仮説（", text)
        self.assertNotIn("最小実験", text)
        decisions.prior(self.home, self.config, "検査の位置が後ろすぎるのではないか", self.tick())
        text = self.render()
        self.assertIn("検査の位置が後ろすぎるのではないか", text)
        self.assertIn("仮説（", text)
        self.assertIn("棄却条件", text)

    def test_skipping_needs_a_reason_and_is_on_record(self):
        with self.assertRaises(HarnessError):
            decisions.skip_prior(self.home, " ", self.tick())
        decisions.skip_prior(self.home, "先週と同じ候補のため", self.tick())
        self.assertIn("先週と同じ候補のため", self.render())
        self.assertIn("最小実験", self.render())

    def test_the_prior_is_kept_with_the_selected_candidate(self):
        decisions.prior(self.home, self.config, "自分の仮説", self.tick())
        self.select(self.first)
        self.assertEqual(self.candidates()[self.first]["human_prior"]["text"], "自分の仮説")

    def test_a_secret_in_the_prior_is_redacted(self):
        token = "ghp_" + "b" * 36
        decisions.prior(self.home, self.config, f"token {token} が原因では", self.tick())
        self.assertNotIn(token, st.ledger_path(self.home).read_text(encoding="utf-8"))


class OnlyAPerson(Decided):
    """Every record that speaks for a person is refused from a scheduled or harness-started process."""

    def test_no_decision_is_recorded_from_an_automated_run(self):
        decisions.search_approve(
            self.home, self.config, self.settings, self.first, ["q one"], "Hanako", "r", self.tick()
        )
        found = {"queries": ["q one"], "prior_art": []}
        before = st.ledger_path(self.home).read_bytes()
        attempts = (
            lambda: decisions.prior(self.home, self.config, "機械が書いた仮説", self.tick()),
            lambda: decisions.skip_prior(self.home, "理由", self.tick()),
            lambda: decisions.resolve(
                self.home, self.first, "rejected", "Hanako", "r", self.tick()
            ),
            lambda: decisions.handoff(
                self.home, self.settings, self.first, "evolution", "Hanako", "r", self.tick()
            ),
            lambda: decisions.search_record(
                self.home, self.config, self.settings, self.first, found, self.tick()
            ),
            lambda: decisions.abandon(self.home, "Hanako", "r", self.tick()),
        )
        for variable in (foundation.AUTOMATED_ENV, foundation.ACTIVE_ENV):
            with mock.patch.dict(os.environ, {variable: "1"}):
                for attempt in attempts:
                    with self.assertRaises(Refusal):
                        attempt()
        self.assertEqual(st.ledger_path(self.home).read_bytes(), before)

    def test_resolve_and_handoff_need_a_named_person(self):
        for reviewer in ("claude", "agent", ""):
            with self.assertRaises(Refusal):
                decisions.resolve(self.home, self.first, "rejected", reviewer, "r", self.tick())
            with self.assertRaises(Refusal):
                decisions.handoff(
                    self.home, self.settings, self.first, "evolution", reviewer, "r", self.tick()
                )
        self.assertEqual(self.candidates()[self.first]["status"], "candidate")

    def test_the_prior_is_written_once_and_never_after_the_hypotheses_were_shown(self):
        decisions.skip_prior(self.home, "先に読む", self.tick())
        with self.assertRaisesRegex(Refusal, "written once"):
            decisions.prior(self.home, self.config, "読んだ後に写した仮説", self.tick())
        self.select(self.first)
        self.assertIsNone(self.candidates()[self.first]["human_prior"])
        self.clock += timedelta(days=7)
        decisions.prior(self.home, self.config, "翌週の仮説", self.tick())
        with self.assertRaises(Refusal):
            decisions.prior(self.home, self.config, "書き直し", self.tick())

    def test_json_report_hides_hypotheses_like_the_markdown_does(self):
        shown = report.as_json(self.state(), self.settings, self.tick())["candidates"]
        self.assertEqual(len(shown), 2)
        for candidate in shown:
            self.assertNotIn("hypothesis", candidate)
            self.assertNotIn("minimal_experiment", candidate)
            self.assertIn("observations", candidate)
        decisions.prior(self.home, self.config, "自分の仮説", self.tick())
        shown = report.as_json(self.state(), self.settings, self.tick())["candidates"]
        self.assertIn("hypothesis", shown[0])

    def test_the_week_is_the_local_week(self):
        tokyo = timezone(timedelta(hours=9))
        monday_morning = datetime(2026, 1, 12, 8, 0, tzinfo=tokyo)  # still Sunday in UTC
        with mock.patch.object(st, "week_of", wraps=st.week_of):
            local = monday_morning.astimezone(tokyo)
            self.assertEqual(local.isocalendar()[1], 3)
            self.assertEqual(monday_morning.astimezone(timezone.utc).isocalendar()[1], 2)
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        try:
            self.assertEqual(st.week_of(monday_morning), "2026-W03")
            later = monday_morning + timedelta(hours=2)
            self.assertEqual(st.week_of(later), "2026-W03")
        finally:
            os.environ.pop("TZ")
            time.tzset()

    def test_a_person_can_close_a_cycle_that_cannot_finish(self):
        self.write("more.md", "classification: synthetic\n## t9\n- [friction] 新しい摩擦\n")
        self.ingest("manual", self.tmp / "more.md")
        pipeline.start_cycle(self.home, self.settings, self.tick())
        with self.assertRaises(Refusal):
            decisions.abandon(self.home, "bot", "r", self.tick())
        result = decisions.abandon(
            self.home, "Hanako", "モデルが形式に合わない出力を返し続ける", self.tick()
        )
        self.assertEqual(result["result"], "abandoned")
        self.assertEqual(pipeline.next_action(self.state())["action"], "none")
        with self.assertRaises(HarnessError):
            decisions.abandon(self.home, "Hanako", "r", self.tick())


class WeeklySelection(Decided):
    def test_weekly_report_is_written_privately_outside_the_repository(self):
        result = decisions.weekly(self.home, self.settings, self.tick())
        self.assertEqual(result["presented"], [self.first, self.second])
        path = result["report"]
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertTrue(path.startswith(str(self.home)))

    def test_one_selection_a_week(self):
        self.assertEqual(self.select(self.first)["status"], "accepted")
        with self.assertRaisesRegex(Refusal, "one per week"):
            self.select(self.second)
        self.clock += timedelta(days=7)
        self.assertEqual(self.select(self.second)["status"], "accepted")

    def test_selection_is_refused_from_a_scheduled_or_harness_process(self):
        for variable in (foundation.AUTOMATED_ENV, foundation.ACTIVE_ENV):
            with mock.patch.dict(os.environ, {variable: "1"}):
                with self.assertRaisesRegex(Refusal, "automated run"):
                    self.select(self.first)
        self.assertEqual(self.candidates()[self.first]["status"], "candidate")

    def test_selection_needs_a_person_and_a_reason(self):
        for reviewer in ("claude", "Insight Bot", ""):
            with self.assertRaises(Refusal):
                self.select(self.first, reviewer=reviewer)
        with self.assertRaises(Refusal):
            decisions.select(self.home, self.settings, self.first, "Hanako", " ", self.tick())

    def test_unknown_candidate(self):
        with self.assertRaisesRegex(HarnessError, "unknown candidate"):
            self.select("INS-20260105-99")


class Transitions(Decided):
    def resolve(self, name, status):
        return decisions.resolve(self.home, name, status, "Hanako", "理由", self.tick())

    def test_candidate_can_only_be_rejected(self):
        with self.assertRaises(Refusal):
            self.resolve(self.first, "tested")
        self.resolve(self.first, "rejected")
        self.assertEqual(self.candidates()[self.first]["status"], "rejected")

    def test_rejected_and_tested_are_final(self):
        self.select(self.first)
        self.resolve(self.first, "tested")
        self.resolve(self.second, "rejected")
        before = json.dumps(self.candidates(), sort_keys=True, ensure_ascii=False)
        for name in (self.first, self.second):
            for status in ("rejected", "tested"):
                with self.assertRaises(Refusal):
                    self.resolve(name, status)
            with self.assertRaises(Refusal):
                self.select(name)
            with self.assertRaises(Refusal):
                decisions.search_approve(
                    self.home, self.config, self.settings, name, ["x"], "Hanako", "r", self.tick()
                )
            with self.assertRaises(Refusal):
                decisions.handoff(
                    self.home, self.settings, name, "evolution", "Hanako", "r", self.tick()
                )
        self.assertEqual(json.dumps(self.candidates(), sort_keys=True, ensure_ascii=False), before)

    def test_the_report_shows_what_was_decided(self):
        self.select(self.first)
        self.resolve(self.first, "tested")
        self.resolve(self.second, "rejected")
        text = self.render()
        self.assertIn("## 選択と結果", text)
        self.assertRegex(text, rf"- {self.first}: tested（2026-01-05、Hanako")
        self.assertRegex(text, rf"- {self.second}: rejected")

    def test_history_records_who_and_why(self):
        self.select(self.first)
        self.resolve(self.first, "tested")
        history = self.candidates()[self.first]["history"]
        self.assertEqual([step["to"] for step in history], ["candidate", "accepted", "tested"])
        self.assertEqual(history[1]["actor"], "Hanako")

    def test_criteria_cannot_be_edited_after_acceptance(self):
        self.select(self.first)
        accepted = self.candidates()[self.first]
        kinds = {record["type"] for record in foundation.read_log(st.ledger_path(self.home))}
        # No record type rewrites a candidate's experiment or criteria.
        self.assertFalse(kinds & {"candidate_updated", "criteria_changed"})
        self.resolve(self.first, "tested")
        after = self.candidates()[self.first]
        for key in ("success_criteria", "kill_criteria", "minimal_experiment"):
            self.assertEqual(after[key], accepted[key])


class Handoffs(LoopCase):
    def routed(self, recommendation, verdict="yes"):
        def alter(output, body):
            output["audits"][0]["recommendation"] = recommendation
            output["audits"][0]["existing_sufficient"] = {
                "verdict": verdict,
                "basis": "手順書の修正で足りる",
                "refs": [],
            }

        return self.run_daily(Backend(audit=alter))

    def test_existing_method_is_enough_goes_to_evolution_and_is_not_presented(self):
        summary, _ = self.routed("handoff_evolution")
        finished = summary["cycles"][0]
        self.assertEqual((len(finished["handoffs"]), len(finished["presented"])), (1, 1))
        (note,) = self.state()["handoffs"]
        self.assertEqual(note["target"], "evolution")
        handed = self.candidates()[note["insight_id"]]
        self.assertEqual(handed["routing"]["disposition"], "handoff_evolution")
        self.assertNotIn("scores", handed)
        stored = json.loads((st.root(self.home) / note["handoff_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["evidence_refs"], handed["evidence_refs"])
        # The note carries the problem and the evidence, and nothing about evaluation criteria.
        self.assertFalse({"criteria", "acceptance", "suite", "targets"} & set(stored))
        shown = decisions.weekly(self.home, self.settings, self.tick())["presented"]
        self.assertNotIn(note["insight_id"], shown)
        with self.assertRaises(Refusal):
            decisions.select(
                self.home, self.settings, note["insight_id"], "Hanako", "r", self.tick()
            )

    def test_improvement_target(self):
        self.routed("handoff_improvement")
        self.assertEqual(self.state()["handoffs"][0]["target"], "improvement")

    def test_the_audit_only_moves_hypotheses_out(self):
        for recommendation, verdict, reason in (
            ("reject", "no", "rejected_by_audit"),
            ("keep", "yes", "existing_method_sufficient"),
        ):
            with self.subTest(recommendation):
                self.setUp()
                summary, _ = self.routed(recommendation, verdict)
                reasons = [item["reason"] for item in summary["cycles"][0]["skipped"]]
                self.assertEqual(reasons, [reason])
                self.assertEqual(len(self.candidates()), 1)

    def test_unfalsifiable_or_misframed_is_not_kept(self):
        def alter(output, body):
            output["audits"][0]["falsifiable"]["verdict"] = "no"
            output["audits"][1]["problem_framing"]["verdict"] = "misframed"

        summary, _ = self.run_daily(Backend(audit=alter))
        reasons = sorted(item["reason"] for item in summary["cycles"][0]["skipped"])
        self.assertEqual(reasons, ["misframed", "not_falsifiable"])
        self.assertEqual(self.candidates(), {})

    def test_findings_without_evidence_and_audits_without_basis_are_dropped(self):
        def alter(output, body):
            finding = {
                "severity": "high",
                "claim": "弱い",
                "evidence": " ",
                "counter_hypothesis": "",
            }
            output["audits"][0]["findings"] = [finding]
            output["audits"][1]["problem_framing"]["basis"] = " "

        self.run_daily(Backend(audit=alter))
        cycle = next(iter(self.state()["cycles"].values()))
        reasons = sorted(item["reason"] for item in cycle["stages"]["audit"]["rejected"])
        self.assertEqual(
            reasons, ["audit_without_basis", "finding_without_evidence", "not_audited"]
        )

    def test_a_person_can_hand_off_an_open_candidate(self):
        self.run_daily()
        name = sorted(self.candidates())[0]
        decisions.handoff(
            self.home,
            self.settings,
            name,
            "improvement",
            "Hanako",
            "成果物の修正で足りる",
            self.tick(),
        )
        self.assertEqual(self.candidates()[name]["routing"]["disposition"], "handoff_improvement")
        self.assertNotIn(name, decisions.weekly(self.home, self.settings, self.tick())["presented"])


class PriorArtSearch(Decided):
    QUERIES = ["shift-left validation rework reduction", "input contract checking pipeline"]

    def setUp(self):
        super().setUp()
        self.configure(
            sources=self.settings["sources"], search={"allowed_domains": ["example.org"]}
        )

    def test_sources_cannot_be_recorded_until_public_sources_are_listed(self):
        self.configure(sources=self.settings["sources"])
        self.approve()
        with self.assertRaisesRegex(Refusal, "allowed_domains"):
            self.found([self.source("partial")])

    def approve(self, queries=None, reviewer="Hanako"):
        return decisions.search_approve(
            self.home,
            self.config,
            self.settings,
            self.first,
            queries or self.QUERIES,
            reviewer,
            "既存手法を確認する",
            self.tick(),
        )

    def found(self, prior_art, queries=None):
        results = {"queries": queries or self.QUERIES, "prior_art": prior_art}
        return decisions.search_record(
            self.home, self.config, self.settings, self.first, results, self.tick()
        )

    def source(self, relation, url="https://example.org/paper"):
        return {"title": "A study", "url": url, "claim": "入口の検査の効果", "relation": relation}

    def test_search_is_never_approved_from_a_scheduled_run(self):
        with mock.patch.dict(os.environ, {foundation.AUTOMATED_ENV: "1"}):
            with self.assertRaises(Refusal):
                self.approve()
        self.assertEqual(self.state()["searches"], {})

    def test_queries_with_secrets_forbidden_terms_or_quoted_evidence_are_not_sent(self):
        deny = self.write("deny.txt", "acme\n")
        self.configure_foundation(privacy={"deny_terms_file": str(deny)})
        quoted = "月次の請求突合バッチを最後まで流してから 対策"
        bad = (
            "acme rework reduction",
            "ghp_" + "c" * 36,
            quoted,
            "billing-reconciliation rework",
            "x" * 121,
        )
        for query in bad:
            with self.subTest(query[:20]):
                with self.assertRaises(Refusal) as refused:
                    self.approve([query])
                self.assertNotIn(query, str(refused.exception))
        self.assertEqual(self.state()["searches"], {})
        with self.assertRaises(Refusal):
            self.approve(["q"] * 6)

    def test_nothing_found_is_reported_as_searched_not_as_new(self):
        self.approve()
        result = self.found([])
        self.assertEqual(result["novelty"]["status"], "searched_none_found")
        decisions.skip_prior(self.home, "テスト", self.tick())
        text = report.render(self.state(), self.settings, self.tick())
        self.assertIn("調べた範囲では見つからなかった（クエリ 2 件、2026-01-05）", text)
        self.assertNotRegex(text, r"新規性が(ある|高い)|前例がない")

    def test_partial_overlap_keeps_the_source_and_the_date(self):
        self.approve()
        self.found([self.source("partial"), self.source("different", "https://example.org/b")])
        candidate = self.candidates()[self.first]
        self.assertEqual(candidate["novelty"]["status"], "partial_overlap")
        self.assertEqual(candidate["prior_art"][0]["url"], "https://example.org/paper")
        self.assertTrue(candidate["prior_art"][0]["retrieved_at"].startswith("2026-01-05"))

    def test_covered_by_existing_work_is_no_longer_presented(self):
        self.approve()
        self.found([self.source("same")])
        shown = decisions.weekly(self.home, self.settings, self.tick())["presented"]
        self.assertEqual(shown, [self.second])

    def test_results_need_an_approved_search_and_proper_sources(self):
        with self.assertRaisesRegex(Refusal, "not approved"):
            self.found([])
        self.approve()
        with self.assertRaisesRegex(Refusal, "not approved"):
            self.found([], queries=["something else"])
        deny = self.write("deny.txt", "acme\n")
        self.configure_foundation(privacy={"deny_terms_file": str(deny)})
        for url in (
            "http://example.org/a",
            "https://user:pw@example.org/a",
            "file:///etc/hosts",
            "https://example.org/acme/wiki",
            "https://example.org/a)[x](https://evil.example",
        ):
            with self.assertRaises(Refusal):
                self.found([self.source("partial", url)])
        with self.assertRaises(HarnessError):
            self.found([{"title": "no url", "claim": "c", "relation": "same"}])
        self.assertEqual(self.candidates()[self.first]["novelty"]["status"], "not_searched")

    def test_allowed_domains_are_enforced_when_set(self):
        self.configure(sources=self.settings["sources"], search={"allowed_domains": ["arxiv.org"]})
        self.approve()
        with self.assertRaisesRegex(Refusal, "allowed_domains"):
            self.found([self.source("partial", "https://evil-arxiv.org/x")])
        self.found([self.source("partial", "https://export.arxiv.org/abs/1")])


if __name__ == "__main__":
    unittest.main()
