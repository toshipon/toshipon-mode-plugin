"""The metadata index: what it accepts, what it reports as a gap, and what it never holds."""

import json

from support import EXAMPLES, ExplorerCase, load

from context_recovery import index as ix
from context_recovery.base import Refusal, state_home


class IndexContract(ExplorerCase):
    def test_entities_are_stamped_with_the_revision_and_hash_of_their_source(self):
        self.index_synthetic()
        entity = self.ok("index", "show", "--type", "decision")["entities"][0]
        self.assertEqual(entity["source"]["revision"], self.base)
        self.assertRegex(entity["source"]["sha256"], "^[0-9a-f]{64}$")
        self.assertGreater(entity["source"]["size"], 0)

    def test_index_holds_no_document_body(self):
        self.index_synthetic()
        stored = (state_home() / "context-explorer").rglob("index.json")
        text = next(stored).read_text(encoding="utf-8")
        for body_line in ("保存量に比例して費用が増えるため", "ledger.charge", "git push --force"):
            self.assertNotIn(body_line, text)

    def test_a_long_or_multiline_description_is_refused(self):
        for description in ("x" * 241, "first line\nsecond line"):
            bad = self.write(
                "bad.json", [{"id": "DOC-1", "type": "document", "description": description}]
            )
            problems = self.refused("index", "add", "--file", bad)["problems"]
            self.assertIn("metadata, not document bodies", problems[0])

    def test_nothing_is_stored_when_one_entity_is_malformed(self):
        entities = [
            {"id": "DOC-ok", "type": "document", "description": "fine"},
            {"id": "DOC-bad", "type": "spreadsheet", "description": "unknown type"},
        ]
        self.refused("index", "add", "--file", self.write("mixed.json", entities))
        self.assertEqual(self.ok("index", "show")["entities"], [])

    def test_malformed_fields_are_each_named(self):
        entity = {
            "id": "RES-1",
            "type": "resource",
            "description": "ok",
            "config_path": "../outside.tf",
            "source": {"locator": "https://example.invalid/page"},
            "relationships": [{"type": "resembles", "to": "RES-2"}],
            "status": "done",
            "body": "a whole document",
        }
        problems = "\n".join(
            self.refused("index", "add", "--file", self.write("e.json", [entity]))["problems"]
        )
        for expected in (
            "unknown field body",
            "config_path",
            "locator must be",
            "relationship must be",
            "status must be",
        ):
            self.assertIn(expected, problems)

    def test_a_source_that_is_not_in_the_repository_is_refused(self):
        entity = {
            "id": "RES-1",
            "type": "resource",
            "description": "x",
            "source": {"locator": "git:infra/missing.tf"},
        }
        self.assertIn(
            "is not in the repository",
            self.refused("index", "add", "--file", self.write("e.json", [entity]))["error"],
        )

    def test_show_is_bounded(self):
        self.index_synthetic()
        shown = self.ok("index", "show", "--limit", "4")
        self.assertEqual(len(shown["entities"]), 4)
        self.assertEqual(shown["more"], 11)

    def test_everything_under_the_state_home_is_private(self):
        self.index_synthetic()
        self.ok("html")
        for path in (self.tmp / "state").rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual((self.tmp / "state").stat().st_mode & 0o777, 0o700)

    def test_scope_names_and_roots_are_validated(self):
        self.assertIn(
            "scope must match", self.run_cli("--scope", "../x", "path", expect=2)[1]["error"]
        )

    def test_state_home_inside_a_repository_is_refused(self):
        import os

        os.environ["AI_HARNESS_HOME"] = str(self.repo / "state")
        self.assertIn("outside any git repository", self.refused("path")["error"])


class Gaps(ExplorerCase):
    def findings(self):
        return {(f["kind"], f["entity"]) for f in self.ok("check")["findings"]}

    def test_conflicting_approved_decisions_are_reported_not_resolved(self):
        self.index_synthetic()
        conflict = next(f for f in self.ok("check")["findings"] if f["kind"] == "conflict")
        self.assertEqual(conflict["entity"], "DEC-0002")
        self.assertIn("both approved, neither supersedes", conflict["detail"])

    def test_a_property_with_no_recorded_rationale_is_a_gap(self):
        self.index_synthetic()
        found = self.findings()
        self.assertIn(("missing_rationale", "PROP-visibility-timeout"), found)
        self.assertNotIn(("missing_rationale", "PROP-retention"), found)
        self.assertNotIn(("missing_rationale", "PROP-max-receive"), found)

    def test_references_go_stale_only_where_the_cited_lines_changed(self):
        self.index_synthetic()
        self.assertFalse([f for f in self.findings() if f[0] == "stale_reference"])
        self.change()
        stale = {entity for kind, entity in self.findings() if kind == "stale_reference"}
        self.assertEqual(stale, {"PROP-retention", "PROP-visibility-timeout", "RES-orders-queue"})

    def test_dangling_references_missing_sources_and_absent_connections_are_recorded(self):
        self.index_synthetic()
        found = self.findings()
        self.assertIn(("dangling_reference", "PROP-max-receive"), found)
        self.assertIn(("no_source", "Q-retention"), found)
        self.assertIn(("source_unavailable", None), found)
        self.assertEqual(
            self.ok("check")["not_checked"], 2
        )  # the PR and the document: not readable from git

    def test_a_deleted_source_is_reported(self):
        self.index_synthetic()
        (self.repo / "src" / "worker.py").unlink()
        import synthetic_repo

        synthetic_repo.commit(self.repo, "synthetic: remove the worker")
        self.assertIn(("missing_source", "CMP-worker"), self.findings())


class Registry(ExplorerCase):
    """Decisions and requirements come from the sre-harness registries; no second schema."""

    def registries(self):
        sha = self.base
        decisions = {
            "decisions": [
                {
                    "id": "DEC-0001",
                    "title": "保持期間は 7 日",
                    "content": "",
                    "rationale": "費用のため",
                    "status": "superseded",
                    "supersedes": [],
                    "evidence": [{"id": "E1", "ref": f"{sha}:docs/adr/0001-retention.md#L6"}],
                },
                {
                    "id": "DCI-0002",
                    "title": "",
                    "content": "保持期間は 14 日",
                    "rationale": " ",
                    "status": "approved",
                    "supersedes": ["DEC-0001"],
                    "evidence": [{"id": "E2", "ref": "llm-calls.jsonl#LC-0001"}],
                },
            ]
        }
        requirements = {
            "requirements": [
                {
                    "id": "REQ-0001",
                    "statement": "14 日間再生できること",
                    "rationale": "監査",
                    "decision_refs": ["DCI-0002"],
                    "evidence": [{"id": "E3", "ref": f"{sha}:docs/requirements.md#L3"}],
                }
            ]
        }
        trace = {
            "links": [
                {
                    "from": {"type": "requirement", "id": "REQ-0001"},
                    "to": {"type": "decision", "id": "DCI-0002"},
                    "relation": "refines",
                },
                {
                    "from": {"type": "iac", "id": "infra/queue.tf"},
                    "to": {"type": "requirement", "id": "REQ-0001"},
                    "relation": "implements",
                },
            ]
        }
        return decisions, requirements, trace

    def test_registry_entries_become_entities_with_their_relations(self):
        decisions, requirements, trace = self.registries()
        added = self.ok(
            "index", "import-registry",
            "--decisions", self.write("decisions.json", decisions),
            "--requirements", self.write("requirements.json", requirements),
            "--trace", self.write("trace.json", trace),
        )  # fmt: skip
        self.assertEqual(added["added"], ["DCI-0002", "DEC-0001", "REQ-0001"])
        entities = {e["id"]: e for e in self.ok("index", "show")["entities"]}
        self.assertEqual(
            entities["DEC-0001"]["source"],
            {"locator": "git:docs/adr/0001-retention.md#L6", "revision": self.base},
        )
        self.assertEqual(entities["DEC-0001"]["status"], "superseded")
        self.assertNotIn("source", entities["DCI-0002"])
        self.assertIn(
            {"type": "supersedes", "to": "DEC-0001"}, entities["DCI-0002"]["relationships"]
        )
        self.assertEqual(
            entities["REQ-0001"]["relationships"],
            [{"type": "decided_by", "to": "DCI-0002"}, {"type": "refines", "to": "DCI-0002"}],
        )
        self.assertIn(
            ("missing_rationale", "DCI-0002"),
            {(f["kind"], f["entity"]) for f in self.ok("check")["findings"]},
        )

    def test_import_needs_a_file(self):
        self.assertIn("at least one", self.refused("index", "import-registry")["error"])

    def test_the_relations_and_statuses_are_the_registries_own(self):
        # The names this skill reuses must stay the ones sre-harness publishes.
        schemas = EXAMPLES.parents[4] / "sre-harness" / "runtime" / "schemas"
        trace = json.loads((schemas / "trace-link.schema.json").read_text(encoding="utf-8"))
        decision = json.loads((schemas / "decision.schema.json").read_text(encoding="utf-8"))
        self.assertLessEqual(set(trace["properties"]["relation"]["enum"]), set(ix.RELATIONS))
        self.assertEqual(tuple(decision["$defs"]["status"]["enum"]), ix.DECISION_STATUSES)


class Sources(ExplorerCase):
    def test_capabilities_are_recorded_as_found_not_assumed(self):
        self.index_synthetic()
        recorded = {s["category"]: s for s in self.ok("overview")["sources"]}
        self.assertFalse(recorded["documents"]["available"])
        self.assertEqual(recorded["git"]["tool"], "git")

    def test_malformed_sources_are_refused(self):
        bad = self.write("s.json", [{"category": "wiki", "available": "yes"}])
        self.assertIn("sources do not follow", self.refused("sources", "--file", bad)["error"])
        self.assertIn(
            "sources do not follow",
            self.refused("sources", "--file", self.write("s2.json", {}))["error"],
        )


class Loading(ExplorerCase):
    def test_unreadable_inputs_are_refusals_not_crashes(self):
        self.assertIn(
            "not found",
            self.refused("index", "add", "--file", str(self.tmp / "nope.json"))["error"],
        )
        self.assertIn(
            "not UTF-8 JSON",
            self.refused("index", "add", "--file", self.write("x.json", "{"))["error"],
        )
        self.assertIn(
            "must be a list",
            self.refused("index", "add", "--file", self.write("y.json", {}))["error"],
        )

    def test_an_index_of_another_version_is_refused(self):
        self.index_synthetic()
        path = self.ok("path")["index"]
        self.write(path, {"schema_version": 99})
        with self.assertRaises(Refusal):
            ix.load(path, self.repo)

    def test_fixture_entities_follow_the_contract(self):
        self.assertEqual([p for e in load("entities.json") for p in ix.check_entity(e)], [])


class ReviewFindings(ExplorerCase):
    """Regressions found by an independent review."""

    def test_secrets_in_a_description_are_replaced_before_the_index_is_stored(self):
        token = "ghp_" + "K3j4" * 9
        entity = {"id": "DOC-1", "type": "document", "description": f"deploy token={token}"}
        self.ok("index", "add", "--file", self.write("e.json", [entity]))
        self.assertNotIn(token, json.dumps(self.ok("index", "show")))
        self.assertNotIn(token, json.dumps(self.ok("explore", "--entity", "DOC-1")))
        self.assertNotIn(
            token, "".join(p.read_text() for p in (self.tmp / "state").rglob("index.json"))
        )

    def test_a_malformed_registry_is_a_refusal(self):
        for name, value in (
            ("decisions", {"decisions": [{"title": "no id"}]}),
            (
                "decisions",
                {"decisions": [{"id": "DEC-0001", "status": "approved", "rationale": None}]},
            ),
            ("trace", {"links": [{"to": {"type": "decision", "id": "DEC-0001"}}]}),
            ("requirements", {"requirements": "all of them"}),
            ("decisions", []),
        ):
            with self.subTest(value):
                refused = self.refused(
                    "index", "import-registry", f"--{name}", self.write("r.json", value)
                )
                self.assertIn("not in the shape sre-harness writes", refused["error"])

    def test_a_source_with_a_stated_revision_is_checked_for_staleness_too(self):
        entity = {
            "id": "PROP-retention",
            "type": "property",
            "description": "保持期間",
            "source": {"locator": "git:infra/queue.tf#L3", "revision": self.base},
            "rationale_recorded": True,
        }
        untouched = {
            **entity,
            "id": "PROP-max",
            "source": {"locator": "git:infra/queue.tf#L5", "revision": self.base},
        }
        self.ok("index", "add", "--file", self.write("e.json", [entity, untouched]))
        self.assertEqual(self.ok("check"), {"findings": [], "head": self.base, "not_checked": 0})
        self.change()
        checked = self.ok("check")
        self.assertEqual(
            [(f["kind"], f["entity"]) for f in checked["findings"]],
            [("stale_reference", "PROP-retention")],
        )
        self.assertEqual(checked["not_checked"], 0)
