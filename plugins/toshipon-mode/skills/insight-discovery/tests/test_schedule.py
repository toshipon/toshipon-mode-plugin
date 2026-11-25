"""Daily and weekly jobs on the foundation's scheduler: dedup, sleep recovery, retry, budget."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from support import EXAMPLES, InsightCase, cli, foundation, st

SCHEDULE = [
    {"kind": "insight_daily", "hour": 3, "minute": 0},
    {"kind": "insight_weekly", "weekday": 0, "hour": 9, "minute": 0},
]
MONDAY = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


class ScheduleCase(InsightCase):
    def setUp(self):
        super().setUp()
        self.configure_foundation(schedule=SCHEDULE)
        self.stub = [sys.executable, str(EXAMPLES / "stub_agent.py")]
        self.adapters(self.stub)
        self.clock = MONDAY

    def adapters(self, argv):
        adapter = {"argv": argv}
        self.configure(
            sources=[{"adapter": "manual", "path": str(EXAMPLES / "records")}],
            agents={"analyst": adapter, "auditor": adapter},
        )

    def tick_at(self, moment):
        """One scheduler tick. The local zone is pinned to UTC so slots are the same everywhere."""
        from insight_discovery import jobs

        queue = foundation.Queue(self.home, self.config["queue"])
        queued = foundation.dispatch.schedule_tick(
            self.home, self.config, queue, moment, local=moment
        )
        done = foundation.dispatch.dispatch(
            self.home, self.config, handlers=jobs.all_handlers(), clock=lambda: moment
        )
        return queued, done

    def jobs(self):
        return foundation.Queue(self.home, self.config["queue"]).jobs()


class DailyAndWeekly(ScheduleCase):
    def test_one_tick_runs_the_daily_analysis_and_the_weekly_report(self):
        queued, done = self.tick_at(MONDAY)
        self.assertEqual(
            queued, ["insight_daily:2026-01-05T03:00", "insight_weekly:2026-01-05T09:00"]
        )
        daily, weekly = done
        self.assertEqual((daily["status"], daily["result"]["llm_calls"]), ("done", 5))
        self.assertEqual(len(weekly["result"]["presented"]), 2)
        self.assertEqual(len(self.state()["candidates"]), 2)
        self.assertTrue(os.path.exists(weekly["result"]["report"]))

    def test_the_same_slot_is_never_queued_or_run_twice(self):
        self.tick_at(MONDAY)
        queued, done = self.tick_at(MONDAY + timedelta(hours=2))
        self.assertEqual((queued, done), ([], []))
        self.assertEqual(len(self.state()["cycles"]), 1)

    def test_days_asleep_come_back_as_one_job(self):
        self.tick_at(MONDAY)
        queued, done = self.tick_at(MONDAY + timedelta(days=4))
        self.assertEqual(queued, ["insight_daily:2026-01-09T03:00"])
        # Nothing new was recorded while asleep: the job ends without a model call.
        self.assertEqual(done[0]["result"]["llm_calls"], 0)
        self.assertEqual(
            len([job for job in self.jobs().values() if job["kind"] == "insight_daily"]), 2
        )

    def test_a_failed_job_keeps_its_progress_and_is_retried_with_backoff(self):
        self.adapters([sys.executable, "-c", "import sys; sys.exit(1)"])
        _, done = self.tick_at(MONDAY)
        failed = self.jobs()["insight_daily:2026-01-05T03:00"]
        self.assertEqual((done[0]["status"], failed["attempts"]), ("pending", 1))
        self.assertGreater(foundation.parse_time(failed["not_before"]), MONDAY)
        self.assertEqual(len(self.state()["evidence"]), 11)  # ingested once, kept
        self.assertEqual(self.state()["open_cycle"], "C-20260105-01")
        self.adapters(self.stub)
        _, done = self.tick_at(MONDAY + timedelta(minutes=5))
        self.assertEqual(done[0]["result"]["llm_calls"], 5)
        self.assertEqual(len(self.state()["evidence"]), 11)
        self.assertEqual(len(self.state()["candidates"]), 2)

    def test_evidence_the_policy_withholds_ends_the_job_without_a_model_call(self):
        self.configure_foundation(
            schedule=SCHEDULE, llm_policy={"allowed_classifications": ["public"]}
        )
        _, done = self.tick_at(MONDAY)
        self.assertEqual(done[0]["status"], "done")
        self.assertEqual(done[0]["result"]["llm_calls"], 0)
        self.assertEqual(done[0]["result"]["next"]["withheld_by_llm_policy"], 11)
        self.assertEqual(self.state()["cycles"], {})

    def test_the_daily_cost_cap_of_the_foundation_stops_the_run_on_record(self):
        adapter = {"claude": {"max_budget_usd": 2}}
        self.configure(
            sources=[{"adapter": "manual", "path": str(EXAMPLES / "records")}],
            agents={"analyst": adapter, "auditor": adapter},
        )
        self.configure_foundation(schedule=SCHEDULE[:1], budget={"max_cost_per_day": 1})
        _, done = self.tick_at(MONDAY)
        self.assertEqual(done[0]["result"]["stopped"]["limit"], "cost")
        self.assertEqual(self.state()["budget_stops"][0]["stage"], "observe")

    def test_the_foundation_entry_point_leaves_insight_jobs_queued(self):
        queue = foundation.Queue(self.home, self.config["queue"])
        foundation.dispatch.schedule_tick(self.home, self.config, queue, MONDAY, local=MONDAY)
        self.assertEqual(foundation.dispatch.dispatch(self.home, self.config), [])
        self.assertEqual({job["status"] for job in self.jobs().values()}, {"pending"})


class EntryPoint(ScheduleCase):
    def cli(self, *args):
        env = {
            **os.environ,
            "AI_HARNESS_HOME": str(self.home),
            "AI_HARNESS_NOW": MONDAY.isoformat(),
        }
        env.pop(foundation.ACTIVE_ENV, None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("builtins.print") as printed:
                code = cli.main(list(args))
                automated = os.environ.get(foundation.AUTOMATED_ENV)
        return code, printed.call_args[0][0], automated

    def test_dry_run_writes_neither_queue_nor_ledger(self):
        code, out, _ = self.cli("schedule-tick", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn('"would_queue"', out)
        self.assertIn('"estimated_llm_calls": 5', out)
        self.assertEqual(self.jobs(), {})
        self.assertFalse(st.ledger_path(self.home).exists())

    def test_tick_and_dispatch_run_as_automation(self):
        code, out, automated = self.cli("schedule-tick")
        self.assertEqual((code, automated), (0, "1"))
        self.assertIn("insight_daily:", out)
        self.assertEqual(len(self.state()["candidates"]), 2)
        code, out, automated = self.cli("dispatch")
        self.assertEqual((code, automated), (0, "1"))
        self.assertIn('"jobs": []', out)

    def test_nothing_runs_inside_a_process_the_harness_started(self):
        with mock.patch.dict(os.environ, {foundation.ACTIVE_ENV: "1"}):
            with self.assertRaisesRegex(foundation.HarnessError, "does not run inside"):
                self.tick_at(MONDAY)


if __name__ == "__main__":
    unittest.main()
