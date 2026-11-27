"""Learning mode: questions tied to evidence, answers kept locally, weak areas asked again first."""

from support import ExplorerCase

from context_recovery.base import state_home


class Learning(ExplorerCase):
    def setUp(self):
        super().setUp()
        self.index_synthetic()

    def question(self, name, kind, entities, excerpt="visibility_timeout_seconds = 45", **changes):
        question = {
            "id": name,
            "kind": kind,
            "entity_ids": entities,
            "prompt": "visibility timeout を worker の処理時間より短くすると何が起きるか",
            "answer": "処理中のメッセージが再配信され、二重に課金されうる",
            "claims": [
                {
                    "id": "C1",
                    "status": "confirmed",
                    "text": "visibility timeout は 45 秒",
                    "references": [
                        {
                            "locator": "git:infra/queue.tf#L4",
                            "revision": self.base,
                            "excerpt": excerpt,
                        }
                    ],
                }
            ],
        }
        return {**question, **changes}

    def add(self, *questions, expect=0):
        return self.run_cli(
            "learn", "add", "--file", self.write("q.json", list(questions)), expect=expect
        )

    def test_questions_need_evidence_that_reads_back(self):
        refused = self.add(
            self.question(
                "Q1",
                "failure_scenario",
                ["PROP-visibility-timeout"],
                excerpt="visibility_timeout_seconds = 999",
            ),
            expect=2,
        )[1]
        self.assertIn("excerpt_not_found", refused["problems"][0])
        self.assertIsNone(self.ok("learn", "next")["question"])

    def test_questions_are_checked_for_shape(self):
        cases = {
            "kind must be one of": self.question("Q1", "trivia", ["PROP-visibility-timeout"]),
            "entity is not in the index": self.question("Q1", "rationale", ["PROP-nope"]),
            "entity_ids must name": self.question("Q1", "rationale", []),
            "prompt is empty": self.question("Q1", "rationale", ["CMP-worker"], prompt=""),
            "at least one claim": self.question("Q1", "rationale", ["CMP-worker"], claims=[]),
            "question id must be": self.question("", "rationale", ["CMP-worker"]),
            "entity_ids must name at least": self.question("Q1", "recognition", [{}]),
        }
        for expected, question in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, "\n".join(self.add(question, expect=2)[1]["problems"]))
        only_unknown = self.question(
            "Q1", "recognition", ["PROP-visibility-timeout"],
            claims=[{"id": "C1", "status": "unknown", "text": "理由", "searched": ["docs/adr/"]}],
        )  # fmt: skip
        self.assertIn("every claim is unknown", self.add(only_unknown, expect=2)[1]["problems"][0])
        self.assertIn(
            "must be a list",
            self.run_cli("learn", "add", "--file", self.write("q.json", {}), expect=2)[1]["error"],
        )
        self.assertIn("question must be an object", self.add("Q1", expect=2)[1]["problems"][0])

    def test_a_reason_nobody_wrote_down_is_not_asked_as_a_rationale_question(self):
        inferred = self.question("Q1", "rationale", ["PROP-visibility-timeout"])
        inferred["claims"][0].update(status="inferred", uncertainty="値からの読み取り")
        refused = self.add(inferred, expect=2)[1]
        self.assertIn(
            "a rationale question needs a source that states the reason", refused["problems"][0]
        )
        self.assertEqual(
            self.add(self.question("Q1", "rationale", ["PROP-visibility-timeout"]))[0]["added"],
            ["Q1"],
        )

    def test_the_answer_is_held_back_until_the_user_has_answered(self):
        self.add(self.question("Q1", "failure_scenario", ["PROP-visibility-timeout", "CMP-worker"]))
        asked = self.ok("learn", "next")["question"]
        self.assertEqual(set(asked), {"id", "kind", "entity_ids", "prompt"})
        recorded = self.ok(
            "learn", "answer", "Q1", "--result", "wrong", "--note", "timeout を延長と誤解"
        )
        self.assertEqual(
            recorded["reveal"]["answer"], "処理中のメッセージが再配信され、二重に課金されうる"
        )
        self.assertEqual(
            recorded["reveal"]["claims"][0]["references"][0]["locator"], "git:infra/queue.tf#L4"
        )

    def test_weak_areas_are_tracked_and_asked_again_first(self):
        self.add(
            self.question("Q1", "failure_scenario", ["PROP-visibility-timeout", "CMP-worker"]),
            self.question("Q2", "recognition", ["PROP-visibility-timeout"]),
            self.question("Q3", "dependency", ["CMP-worker"]),
        )
        self.assertEqual(
            self.add(self.question("Q1", "rationale", ["CMP-worker"]))[0],
            {"added": [], "skipped": ["Q1"]},
        )
        self.assertEqual(self.ok("learn", "next")["question"]["id"], "Q1")
        self.ok("learn", "answer", "Q1", "--result", "wrong")
        self.ok("learn", "answer", "Q2", "--result", "correct")
        self.ok("learn", "answer", "Q3", "--result", "correct")
        weak = self.ok("learn", "weak")
        self.assertEqual((weak["questions"], weak["answered"]), (3, 3))
        self.assertEqual(
            weak["entities"]["PROP-visibility-timeout"],
            {"attempts": 2, "accuracy": 0.5, "weak": True},
        )
        self.assertTrue(weak["entities"]["CMP-worker"]["weak"])
        self.assertEqual(
            weak["kinds"]["failure_scenario"], {"attempts": 1, "accuracy": 0.0, "weak": True}
        )
        self.assertEqual(
            weak["kinds"]["recognition"], {"attempts": 1, "accuracy": 1.0, "weak": False}
        )
        self.assertEqual(
            self.ok("learn", "next")["question"]["id"], "Q1"
        )  # the missed one comes back
        self.ok("learn", "answer", "Q1", "--result", "correct")
        self.assertIsNone(self.ok("learn", "next")["question"])
        # one right answer after a miss is 1 in 2: still below two in three, so still weak
        self.assertEqual(
            self.ok("learn", "weak")["kinds"]["failure_scenario"],
            {"attempts": 2, "accuracy": 0.5, "weak": True},
        )

    def test_unknown_questions_and_results_are_refused(self):
        self.assertIn(
            "no such question",
            self.refused("learn", "answer", "Q9", "--result", "correct")["error"],
        )

    def test_records_stay_in_the_local_state_home(self):
        self.add(self.question("Q1", "recognition", ["PROP-visibility-timeout"]))
        self.ok("learn", "answer", "Q1", "--result", "partial")
        logs = list(state_home().rglob("learning.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.repo.rglob("learning.jsonl")))
