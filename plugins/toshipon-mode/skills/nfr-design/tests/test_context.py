"""Context provenance, review boundaries, conflict and change-impact regressions."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "runtime"))
from harness.claims import (  # noqa: E402
    assess_context,
    diff_context,
    extract_claims,
    prepare_input,
    review_claim,
    validate_claims,
)
from harness.intake import collect, load_context  # noqa: E402
from harness.relations import relate_claims  # noqa: E402
from harness.runner import claude_backend, demo_backend, run_loop  # noqa: E402
from harness.workspace import load_workspace  # noqa: E402


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nfr context ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "meeting.md").write_text("受注 API の Region 障害 RTO は 30 分を希望する。")
        (self.root / "wiki.md").write_text("受注 API の Region 障害 RTO は 120 分とする。")
        (self.root / "resources.json").write_text(
            '{"DBInstances":[{"MultiAZ":false,"BackupRetentionPeriod":7}]}'
        )
        (self.root / "aws.md").write_text("RTO と RPO を定義して復旧方式を検証する。")
        self.manifest = {
            "project": "受注 API",
            "region": "ap-northeast-1",
            "budget_monthly_usd": 1500,
            "sources": [],
        }
        for index, name, kind, locator, version in [
            (1, "meeting.md", "meeting", "meeting://order/2026-11-03", "2"),
            (2, "wiki.md", "confluence", "https://example.atlassian.net/wiki/pages/123", "1"),
            (3, "resources.json", "aws", "aws://rds/ap-northeast-1", "snapshot-1"),
            (
                4,
                "aws.md",
                "file",
                "https://docs.aws.amazon.com/wellarchitected/latest/framework/rel-13.html",
                "2026-11-03",
            ),
        ]:
            self.manifest["sources"].append(
                {
                    "id": f"S{index}",
                    "kind": kind,
                    "title": name,
                    "adapter": "file",
                    "locator": locator,
                    "path": name,
                    "version": version,
                    "owner": "service-owner",
                }
            )
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.ws = self.root / "workspace"
        self.context = collect(self.manifest_path, self.ws)
        self.evidence = self.root / "evidence.json"
        self.evidence.write_text(
            json.dumps(
                [
                    {
                        "id": "E1",
                        "url": self.manifest["sources"][3]["locator"],
                        "claim": "Synthetic recovery guidance fixture",
                        "status": "verified",
                        "verified_by": "test reviewer",
                        "checked_at": "2026-11-03",
                    }
                ]
            )
        )

    def proposals(self):
        claims = []
        for i, value, kind, metric, unit, quote in [
            (
                1,
                30,
                "requirement",
                "rto_minutes",
                "minutes",
                "受注 API の Region 障害 RTO は 30 分を希望する。",
            ),
            (
                2,
                120,
                "requirement",
                "rto_minutes",
                "minutes",
                "受注 API の Region 障害 RTO は 120 分とする。",
            ),
            (3, False, "observed", "db_multi_az", "boolean", '"MultiAZ":false'),
        ]:
            source = self.context["sources"][i - 1]
            claims.append(
                {
                    "id": f"C{i}",
                    "kind": kind,
                    "subject": "order-api",
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "scope": "region failure",
                    "period": "per incident",
                    "summary": quote,
                    "source_refs": [
                        {
                            "source_id": source["id"],
                            "quote": quote,
                            "content_sha256": source["content_sha256"],
                        }
                    ],
                }
            )
        return {"claims": claims}

    def extract(self, proposals=None):
        result = self.proposals() if proposals is None else proposals
        return extract_claims(self.ws, lambda role, payload, schema: copy.deepcopy(result))

    def approve_one(self):
        self.extract()
        review_claim(
            self.ws,
            "C1",
            "approved",
            "service-owner",
            "確認した業務目標",
            requirement_id="REQ-001",
            supersedes=["C2"],
        )
        return prepare_input(self.ws, self.evidence, self.ws / "input.json")

    def test_collection_preserves_versions_hashes_and_originals(self):
        loaded = load_context(self.ws)
        self.assertEqual(loaded["sources"][0]["version"], "2")
        self.assertEqual(
            (self.root / "meeting.md").read_text(),
            "受注 API の Region 障害 RTO は 30 分を希望する。",
        )
        self.assertEqual(len(loaded["sources"]), 4)
        self.assertTrue((self.ws / "sources.tsv").is_file())
        with self.assertRaises(ValueError):
            collect(self.manifest_path, self.ws)

    def test_snapshot_tampering_rejected(self):
        (self.ws / "sources/S1.txt").write_text("altered")
        with self.assertRaises(ValueError):
            load_context(self.ws)

    def test_extractor_cannot_approve_or_fabricate_quote(self):
        proposed = self.proposals()
        proposed["claims"][0].update(approval={"status": "approved"})
        with self.assertRaises(ValueError):
            self.extract(proposed)
        self.assertFalse((self.ws / "claims.json").exists())
        proposed = self.proposals()
        for claim in proposed["claims"]:
            claim["source_refs"][0].update(quote="not in source")
        with self.assertRaisesRegex(ValueError, "CLAIM_QUOTE: C1"):
            self.extract(proposed)
        self.assertFalse((self.ws / "claims.json").exists())
        self.assertFalse((self.ws / "extraction-rejected.json").exists())

    def test_unmatched_quote_or_hash_drops_only_that_claim_and_records_it(self):
        proposed = self.proposals()
        proposed["claims"][0]["source_refs"][0].update(quote="not in source")
        proposed["claims"][2]["source_refs"][0].update(content_sha256="0" * 64)
        result = self.extract(proposed)
        self.assertEqual([c["id"] for c in result["claims"]], ["C2"])
        self.assertEqual(json.loads((self.ws / "claims.json").read_text()), result)
        rejected = json.loads((self.ws / "extraction-rejected.json").read_text())["rejected"]
        self.assertEqual([r["claim"]["id"] for r in rejected], ["C1", "C3"])
        self.assertTrue(rejected[0]["findings"][0].startswith("CLAIM_QUOTE"))
        self.assertTrue(rejected[1]["findings"][0].startswith("CLAIM_STALE"))
        self.assertTrue(all(r["claim"]["approval"]["status"] == "unreviewed" for r in rejected))

    def test_tampered_snapshot_is_never_dropped_as_a_bad_claim(self):
        snapshot = self.ws / self.context["sources"][0]["snapshot_path"]

        def backend(role, payload, schema):
            snapshot.write_text("altered")
            return self.proposals()

        with self.assertRaisesRegex(ValueError, "changed during extraction"):
            extract_claims(self.ws, backend)
        self.assertFalse((self.ws / "claims.json").exists())

    def test_batched_extraction_splits_whole_sources_and_renumbers(self):
        (self.root / "big.md").write_text("大" * 7000)
        self.manifest["sources"].append(
            {**self.manifest["sources"][3], "id": "S5", "title": "big.md", "path": "big.md"}
        )
        self.manifest_path.write_text(json.dumps(self.manifest))
        ws = self.root / "batched"
        context = collect(self.manifest_path, ws)
        by_id = {s["id"]: s for s in context["sources"]}
        calls = []

        def backend(role, payload, schema):
            ids = [s["id"] for s in payload["sources"]]
            calls.append(ids)
            claims = []
            for sid in ids:
                text = (ws / by_id[sid]["snapshot_path"]).read_text()
                claims.append(
                    {
                        "id": "C1",
                        "kind": "unknown",
                        "subject": sid,
                        "metric": "note",
                        "value": "x",
                        "unit": "text",
                        "scope": "unknown",
                        "period": "unknown",
                        "summary": sid,
                        "source_refs": [
                            {
                                "source_id": sid,
                                "quote": text[:5],
                                "content_sha256": by_id[sid]["content_sha256"],
                            }
                        ],
                    }
                )
            return {"claims": claims}

        result = extract_claims(ws, backend, batch_bytes=16 * 1024, jobs=2)
        self.assertEqual(sorted(sum(calls, [])), ["S1", "S2", "S3", "S4", "S5"])
        self.assertIn(["S5"], calls)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c["id"] for c in result["claims"]], ["C1", "C2", "C3", "C4", "C5"])
        self.assertEqual(validate_claims(ws, result), [])
        for bad in [{"batch_bytes": 1000}, {"jobs": 0}, {"jobs": True}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                extract_claims(self.ws, backend, **bad)

    def test_extraction_defaults_to_unreviewed(self):
        result = self.extract()
        self.assertTrue(all(c["approval"]["status"] == "unreviewed" for c in result["claims"]))
        self.assertEqual(validate_claims(self.ws, result), [])
        with self.assertRaises(ValueError):
            self.extract()

    def test_latest_source_does_not_silently_win(self):
        self.extract()
        report = assess_context(self.ws)
        self.assertFalse(report["passed"])
        self.assertTrue(report["conflicts"])
        with self.assertRaises(ValueError):
            prepare_input(self.ws, self.evidence, self.ws / "input.json")

    def test_review_resolves_conflict_and_observed_is_not_requirement(self):
        data = self.approve_one()
        self.assertEqual(len(data["requirements"]), 1)
        req = data["requirements"][0]
        self.assertEqual((req["id"], req["target"]), ("REQ-001", 30))
        self.assertEqual(req["context_refs"], ["C1"])
        self.assertEqual(req["source_refs"], ["S1"])
        self.assertIn("context_binding", data)
        self.assertTrue(assess_context(self.ws)["passed"])

    def test_approval_does_not_resolve_conflict_by_itself(self):
        self.extract()
        review_claim(
            self.ws,
            "C1",
            "approved",
            "owner",
            "Approved requested target",
            requirement_id="REQ-001",
        )
        self.assertFalse(assess_context(self.ws)["passed"])

    def test_review_requires_identity_reason_and_stable_requirement_id(self):
        self.extract()
        for reviewer, reason, rid in [
            ("", "reason", "REQ-001"),
            ("owner", "", "REQ-001"),
            ("owner", "reason", None),
        ]:
            with (
                self.subTest(reviewer=reviewer, reason=reason, rid=rid),
                self.assertRaises(ValueError),
            ):
                review_claim(self.ws, "C1", "approved", reviewer, reason, requirement_id=rid)
        review_claim(
            self.ws,
            "C1",
            "approved",
            "owner",
            "reason",
            requirement_id="REQ-001",
            supersedes=["C2"],
        )
        with self.assertRaises(ValueError):
            review_claim(self.ws, "C1", "approved", "owner", "renumber", requirement_id="REQ-002")

    def test_unrelated_claim_cannot_be_superseded(self):
        self.extract()
        with self.assertRaises(ValueError):
            review_claim(
                self.ws,
                "C1",
                "approved",
                "owner",
                "discard current resource",
                requirement_id="REQ-001",
                supersedes=["C3"],
            )

    def test_approval_is_bound_to_claim_content(self):
        self.approve_one()
        data = json.loads((self.ws / "claims.json").read_text())
        data["claims"][0]["value"] = 1
        self.assertTrue(validate_claims(self.ws, data))

    def test_runner_uses_approved_context_and_freezes_source_content(self):
        data = self.approve_one()
        context = load_workspace(self.ws, data)
        self.assertFalse(context["findings"], context["findings"])
        self.assertIn("sources/S1.txt", context["hashes"])
        self.assertTrue(run_loop(data, self.ws / "run", demo_backend, 3, workspace=self.ws))
        self.assertTrue((self.ws / "run/context.json").exists())
        self.assertTrue((self.ws / "run/claims.json").exists())
        self.assertIn("S1", (self.ws / "run/context-report.md").read_text())

    def test_manual_input_cannot_bypass_context_gate(self):
        data = self.approve_one()
        data.pop("context_binding")
        data["requirements"][0].pop("context_refs")
        self.assertTrue(load_workspace(self.ws, data)["findings"])

    def test_context_target_drift_blocks_design(self):
        data = self.approve_one()
        data["requirements"][0]["target"] = 120
        self.assertTrue(load_workspace(self.ws, data)["findings"])

    def test_source_changes_map_to_requirements_decisions_and_tests(self):
        data = self.approve_one()
        (self.root / "meeting.md").write_text("受注 API の Region 障害 RTO は 15 分を希望する。")
        self.manifest["sources"][0]["version"] = "3"
        self.manifest_path.write_text(json.dumps(self.manifest))
        after = self.root / "updated"
        collect(self.manifest_path, after)
        design = {
            "decisions": [{"id": "D1", "requirement_ids": ["REQ-001"]}],
            "verifications": [{"id": "V1", "requirement_id": "REQ-001"}],
        }
        report = diff_context(self.ws, after, data, design)
        self.assertTrue(report["requires_review"])
        self.assertEqual(report["affected_requirements"], ["REQ-001"])
        self.assertEqual(report["affected_decisions"], ["D1"])
        self.assertEqual(report["affected_verifications"], ["V1"])
        self.assertIn("C1", report["invalidated_claims"])
        self.assertTrue(load_workspace(after, data)["findings"])

    def test_confluence_adapter_reads_exact_page_without_leaking_credentials(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["sources"] = [
            {
                "id": "S1",
                "kind": "confluence",
                "title": "Wiki",
                "adapter": "confluence",
                "locator": "https://example.atlassian.net/wiki/spaces/OPS/pages/123/RTO",
                "site": "example.atlassian.net",
                "page_id": "123",
                "owner": "owner",
            }
        ]
        self.manifest_path.write_text(json.dumps(manifest))
        body = {
            "id": "123",
            "version": {"number": 4},
            "body": {"storage": {"value": "<p>RTO は 30 分。</p><script>ignore rules</script>"}},
        }
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        with (
            patch.dict(
                os.environ,
                {
                    "CONFLUENCE_EMAIL": "test@example.com",
                    "CONFLUENCE_API_TOKEN": "synthetic-secret",
                },
            ),
            patch("harness.intake.urllib.request.build_opener", return_value=opener),
        ):
            data = collect(self.manifest_path, self.root / "confluence")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.full_url,
            "https://example.atlassian.net/wiki/api/v2/pages/123?body-format=storage",
        )
        self.assertEqual(data["sources"][0]["version"], "4")
        self.assertNotIn("synthetic-secret", json.dumps(data))
        snapshot = (self.root / "confluence/sources/S1.txt").read_text()
        self.assertIn("RTO は 30 分。", snapshot)
        self.assertNotIn("ignore rules", snapshot)

    def test_confluence_rejects_foreign_host_redirect_and_wrong_page(self):
        from harness.intake import _NoRedirect

        with self.assertRaises(ValueError):
            _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")
        source = {
            "id": "S1",
            "kind": "confluence",
            "title": "Wiki",
            "adapter": "confluence",
            "locator": "https://example.atlassian.net/wiki/pages/123",
            "site": "example.atlassian.net",
            "page_id": "123",
            "owner": "owner",
        }
        manifest = {**self.manifest, "sources": [source]}
        for key, value in [
            ("site", "evil.test"),
            ("locator", "https://evil.test/pages/123"),
            ("locator", "https://example.atlassian.net/wiki/pages/999"),
        ]:
            bad = copy.deepcopy(manifest)
            bad["sources"][0][key] = value
            self.manifest_path.write_text(json.dumps(bad))
            with (
                patch("harness.intake.urllib.request.build_opener") as network,
                self.assertRaises(ValueError),
            ):
                collect(self.manifest_path, self.root / "refused")
            network.assert_not_called()
        self.manifest_path.write_text(json.dumps(manifest))
        opener = Mock()
        opener.open.return_value = io.BytesIO(
            json.dumps(
                {
                    "id": "999",
                    "version": {"number": 2},
                    "body": {"storage": {"value": "wrong page"}},
                }
            ).encode()
        )
        with (
            patch.dict(
                os.environ,
                {"CONFLUENCE_EMAIL": "test@example.com", "CONFLUENCE_API_TOKEN": "dummy"},
            ),
            patch("harness.intake.urllib.request.build_opener", return_value=opener),
            self.assertRaises(ValueError),
        ):
            collect(self.manifest_path, self.root / "wrong-page")

    def test_aws_adapter_is_read_only_and_ignores_response_order(self):
        manifest = {
            **self.manifest,
            "sources": [
                {
                    "id": "S1",
                    "kind": "aws",
                    "title": "RDS snapshot",
                    "adapter": "aws-cli",
                    "locator": "aws://rds/ap-northeast-1",
                    "service": "rds",
                    "owner": "owner",
                }
            ],
        }
        self.manifest_path.write_text(json.dumps(manifest))
        one = {
            "DBInstances": [
                {"DBInstanceIdentifier": "a", "MultiAZ": False},
                {"DBInstanceIdentifier": "b", "MultiAZ": True},
            ]
        }
        two = {"DBInstances": list(reversed(one["DBInstances"]))}
        contexts = []
        for index, response in enumerate([one, two]):
            with patch(
                "harness.intake.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(response).encode(), b""),
            ) as call:
                contexts.append(collect(self.manifest_path, self.root / f"aws-{index}"))
            self.assertEqual(call.call_args.args[0][:3], ["aws", "rds", "describe-db-instances"])
            self.assertNotIn("shell", call.call_args.kwargs)
            self.assertEqual(
                call.call_args.kwargs["env"]["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"], "true"
            )
        self.assertEqual(
            contexts[0]["sources"][0]["content_sha256"], contexts[1]["sources"][0]["content_sha256"]
        )
        self.assertEqual(contexts[0]["sources"][0]["version"], contexts[1]["sources"][0]["version"])
        with (
            patch(
                "harness.intake.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, b"", b"secret diagnostics"),
            ),
            self.assertRaisesRegex(ValueError, "AWS read command failed"),
        ):
            collect(self.manifest_path, self.root / "aws-error")
        self.assertFalse((self.root / "aws-error").exists())

    def test_source_and_output_symlinks_and_size_limits_rejected(self):
        (self.root / "linked.md").symlink_to(self.root / "meeting.md")
        manifest = {
            **self.manifest,
            "sources": [{**self.manifest["sources"][0], "path": "linked.md"}],
        }
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            collect(self.manifest_path, self.root / "symlink-source")
        self.manifest_path.write_text(json.dumps(self.manifest))
        (self.root / "output-link").symlink_to(self.ws, target_is_directory=True)
        with self.assertRaises(ValueError):
            collect(self.manifest_path, self.root / "output-link")
        with patch("harness.intake.MAX_SOURCE_BYTES", 16), self.assertRaises(ValueError):
            collect(self.manifest_path, self.root / "too-large")

    def test_same_body_new_version_requires_explicit_rereview(self):
        self.approve_one()
        metadata = json.loads((self.ws / "context.json").read_text())
        metadata["sources"][0]["version"] = "3"
        (self.ws / "context.json").write_text(json.dumps(metadata))
        self.assertTrue(
            any(f["id"] == "CLAIM_REVIEW_STALE" for f in assess_context(self.ws)["findings"])
        )
        review_claim(self.ws, "C1", "approved", "owner", "新しい版の同じ内容を確認")
        self.assertTrue(assess_context(self.ws)["passed"])

    def test_changed_observed_source_conservatively_impacts_design(self):
        data = self.approve_one()
        (self.root / "resources.json").write_text('{"DBInstances":[{"MultiAZ":true}]}')
        after = self.root / "resource-updated"
        collect(self.manifest_path, after)
        report = diff_context(
            self.ws,
            after,
            data,
            {
                "decisions": [{"id": "D1", "requirement_ids": ["REQ-001"]}],
                "verifications": [{"id": "V1", "requirement_id": "REQ-001"}],
            },
        )
        self.assertEqual(report["affected_requirements"], ["REQ-001"])
        self.assertEqual(report["affected_decisions"], ["D1"])
        self.assertEqual(report["potentially_affected_requirements"], ["REQ-001"])
        self.assertEqual(report["directly_affected_requirements"], [])

    def test_superseded_source_change_invalidates_resolution(self):
        data = self.approve_one()
        (self.root / "wiki.md").write_text("RTO を 10 分に変更する。")
        after = self.root / "wiki-updated"
        collect(self.manifest_path, after)
        self.assertIn("C1", diff_context(self.ws, after, data)["invalidated_claims"])

    def test_disjoint_scope_is_not_compared_as_conflict(self):
        proposed = self.proposals()
        proposed["claims"][1]["scope"] = "AZ failure"
        self.extract(proposed)
        self.assertEqual(assess_context(self.ws)["conflicts"], [])

    def test_relate_lists_differently_named_claims_without_changing_assess(self):
        self.extract()
        before = assess_context(self.ws)
        output = {
            "relations": [
                {
                    "claim_ids": ["C3", "C1"],
                    "relation": "possible_conflict",
                    "reason": "単一 AZ の DB で Region 障害の RTO 30 分を満たす根拠が無い。",
                },
                {
                    "claim_ids": ["C1", "C2"],
                    "relation": "possible_conflict",
                    "reason": "assess が同じ group として比較済み。",
                },
            ]
        }
        result = relate_claims(
            self.ws, lambda role, payload, schema: copy.deepcopy(output), self.root / "rel"
        )
        self.assertEqual([r["claim_ids"] for r in result["relations"]], [["C1", "C3"]])
        self.assertEqual(result["relations"][0]["status"], "candidate")
        self.assertEqual(result["relations"][0]["source_ids"], ["S1", "S3"])
        self.assertIn("食い違いの候補", (self.root / "rel/relations.md").read_text())
        self.assertEqual(assess_context(self.ws), before)

    def test_relate_payload_carries_quotes_but_no_review_fields(self):
        self.extract()
        seen = {}

        def backend(role, payload, schema):
            seen.update(role=role, payload=payload)
            return {"relations": []}

        relate_claims(self.ws, backend, self.root / "rel")
        self.assertEqual(seen["role"], "relator")
        item = seen["payload"]["claims"][0]
        self.assertNotIn("approval", item)
        self.assertEqual(item["sources"][0]["source_id"], "S1")
        self.assertIn("候補はありません", (self.root / "rel/relations.md").read_text())

    def test_relate_rejects_unknown_claims_and_reused_output(self):
        self.extract()
        bad = {"relations": [{"claim_ids": ["C1", "C9"], "relation": "overlap", "reason": "x"}]}
        with self.assertRaisesRegex(ValueError, "unknown claims"):
            relate_claims(self.ws, lambda *a: copy.deepcopy(bad), self.root / "rel")
        used = self.root / "used"
        used.mkdir()
        (used / "keep.txt").write_text("x")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            relate_claims(self.ws, lambda *a: {"relations": []}, used)
        single = {"relations": [{"claim_ids": ["C1"], "relation": "overlap", "reason": "x"}]}
        with self.assertRaisesRegex(ValueError, "relations schema"):
            relate_claims(self.ws, lambda *a: copy.deepcopy(single), self.root / "rel2")

    def test_claude_timeout_is_configurable_and_bounded(self):
        ok = subprocess.CompletedProcess(
            [], 0, '{"subtype":"success","structured_output":{"relations":[]}}', ""
        )
        with patch("harness.runner.subprocess.run", return_value=ok) as call:
            claude_backend("relator", {}, {"type": "object"}, timeout=900)
            self.assertEqual(call.call_args.kwargs["timeout"], 900)
            claude_backend("extractor", {}, {"type": "object"})
            self.assertEqual(call.call_args.kwargs["timeout"], 180)
        for value in (10, 7200, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Timeout"):
                claude_backend("extractor", {}, {}, timeout=value)

    def test_cli_timeout_option_reaches_backend_and_rejects_out_of_range(self):
        from contextlib import redirect_stderr, redirect_stdout

        from harness.__main__ import main

        def cli(args):
            with (
                patch.object(sys, "argv", ["nfr-harness", *args]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                try:
                    return main()
                except SystemExit as error:
                    return error.code

        self.assertEqual(cli(["extract", "--workspace", str(self.ws), "--timeout-seconds", "5"]), 2)
        with patch(
            "harness.runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 600),
        ) as call:
            code = cli(["extract", "--workspace", str(self.ws), "--timeout-seconds", "600"])
        self.assertEqual(code, 2)
        self.assertEqual(call.call_args.kwargs["timeout"], 600)
        self.assertFalse((self.ws / "claims.json").exists())
        self.assertEqual(cli(["extract", "--workspace", str(self.ws), "--batch-bytes", "100"]), 2)
        self.assertEqual(cli(["extract", "--workspace", str(self.ws), "--jobs", "9"]), 2)
        proposed = self.proposals()
        proposed["claims"][0]["source_refs"][0].update(quote="not in source")
        proposals = self.root / "proposals.json"
        proposals.write_text(json.dumps(proposed))
        code = cli(
            ["extract", "--workspace", str(self.ws), "--proposals", str(proposals), "--jobs", "2"]
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.ws / "extraction-rejected.json").is_file())

    def test_context_cli_flow_without_model(self):
        from contextlib import redirect_stderr, redirect_stdout

        from harness.__main__ import main

        root = self.root / "cli-workspace"
        proposals = self.root / "proposals.json"
        proposals.write_text(json.dumps(self.proposals()))

        def cli(args):
            with (
                patch.object(sys, "argv", ["nfr-harness", *args]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                return main()

        self.assertEqual(
            cli(["collect", "--manifest", str(self.manifest_path), "--out", str(root)]), 0
        )
        self.assertEqual(
            cli(["extract", "--workspace", str(root), "--proposals", str(proposals)]), 0
        )
        self.assertEqual(
            cli(["assess", "--workspace", str(root), "--out", str(root / "before-review")]), 1
        )
        self.assertEqual(
            cli(
                [
                    "review",
                    "--workspace",
                    str(root),
                    "--claim",
                    "C1",
                    "--status",
                    "approved",
                    "--reviewer",
                    "owner",
                    "--reason",
                    "業務目標を確認",
                    "--requirement-id",
                    "REQ-001",
                    "--supersedes",
                    "C2",
                ]
            ),
            0,
        )
        self.assertEqual(
            cli(
                [
                    "prepare",
                    "--workspace",
                    str(root),
                    "--evidence",
                    str(self.evidence),
                    "--out",
                    str(root / "input.json"),
                ]
            ),
            0,
        )
        self.assertEqual(
            cli(["assess", "--workspace", str(root), "--out", str(root / "after-review")]), 0
        )
        self.assertEqual(
            cli(
                [
                    "diff",
                    "--before",
                    str(root),
                    "--after",
                    str(self.ws),
                    "--input",
                    str(root / "input.json"),
                    "--out",
                    str(self.root / "diff-report"),
                ]
            ),
            0,
        )
        self.assertTrue((self.root / "diff-report/impact.md").is_file())

    def test_nonblocking_context_findings_do_not_turn_pass_into_fail(self):
        proposed = self.proposals()
        proposal = copy.deepcopy(proposed["claims"][0])
        proposal.update(id="C4", kind="proposal")
        proposed["claims"].append(proposal)
        self.extract(proposed)
        review_claim(
            self.ws,
            "C1",
            "approved",
            "owner",
            "確認した目標",
            requirement_id="REQ-001",
            supersedes=["C2"],
        )
        data = prepare_input(self.ws, self.evidence, self.ws / "input.json")
        self.assertTrue(run_loop(data, self.ws / "with-medium", demo_backend, 3, workspace=self.ws))
        report = json.loads((self.ws / "with-medium/evaluation.json").read_text())
        self.assertTrue(report["passed"])
        self.assertTrue(any(f["severity"] == "medium" for f in report["findings"]))

    def test_superseded_source_version_also_stales_approved_resolution(self):
        self.approve_one()
        metadata = json.loads((self.ws / "context.json").read_text())
        metadata["sources"][1]["version"] = "2"
        (self.ws / "context.json").write_text(json.dumps(metadata))
        findings = assess_context(self.ws)["findings"]
        self.assertTrue(
            any(
                f["id"] == "CLAIM_REVIEW_STALE" and "C1" in f.get("claim_ids", []) for f in findings
            )
        )

    def test_diff_rejects_unrelated_baseline_and_extraction_budget(self):
        data = self.approve_one()
        data["project"] = "another-project"
        with self.assertRaises(ValueError):
            diff_context(self.ws, self.ws, data)
        clean = self.root / "small-budget"
        collect(self.manifest_path, clean)
        with patch("harness.claims.MAX_EXTRACTION_BYTES", 16), self.assertRaises(ValueError):
            extract_claims(clean, lambda *args: self.fail("backend must not run past the budget"))

    def test_unknown_measurement_scope_does_not_become_approved_fact(self):
        proposed = self.proposals()
        proposed["claims"][0]["period"] = "unknown"
        self.extract(proposed)
        review_claim(self.ws, "C1", "approved", "owner", "value reviewed", requirement_id="REQ-001")
        self.assertTrue(
            any(f["id"] == "CLAIM_SCOPE_UNRESOLVED" for f in assess_context(self.ws)["findings"])
        )
        with self.assertRaises(ValueError):
            prepare_input(self.ws, self.evidence, self.ws / "unknown-period-input.json")

    def test_unchanged_collection_is_not_a_false_change(self):
        data = self.approve_one()
        after = self.root / "unchanged"
        collect(self.manifest_path, after)
        report = diff_context(self.ws, after, data)
        self.assertFalse(report["requires_review"])
        self.assertFalse(report["changed_sources"])


if __name__ == "__main__":
    unittest.main()
