"""Evidence accuracy: a claim holds only when its excerpt is in the cited source at the cited revision."""

from pathlib import Path

from support import ExplorerCase

from context_recovery import claims as cl
from context_recovery import gitread
from context_recovery.base import Refusal


class ClaimContract(ExplorerCase):
    def base_claim(self, **changes):
        claim = {
            "id": "C1",
            "status": "confirmed",
            "text": "保持期間は 7 日",
            "references": [
                {
                    "locator": "git:infra/queue.tf#L3",
                    "revision": self.base,
                    "excerpt": "message_retention_days     = 7",
                }
            ],
        }
        return {**claim, **changes}

    def test_a_well_formed_claim_has_no_problems(self):
        self.assertEqual(cl.check_claim(self.base_claim()), [])

    def test_each_status_names_what_it_must_carry(self):
        cases = {
            "confirmed needs a supporting excerpt": self.base_claim(
                references=[{"locator": "git:infra/queue.tf", "revision": self.base}]
            ),
            "confirmed needs a source locator and revision": self.base_claim(references=[]),
            "inferred must say what is uncertain": self.base_claim(status="inferred"),
            "conflict needs an excerpt from each": self.base_claim(
                status="conflict", uncertainty="?"
            ),
            "unknown must list what was searched": self.base_claim(
                status="unknown", references=[], searched=[]
            ),
            "status must be one of": self.base_claim(status="likely"),
            "text is empty": self.base_claim(text=" "),
            "claim id must look like C1": self.base_claim(id="claim-1"),
            "revision must be a commit sha": self.base_claim(
                references=[
                    {"locator": "git:infra/queue.tf", "revision": "main", "excerpt": "retention"}
                ]
            ),
            "locator must be": self.base_claim(
                references=[{"locator": "infra/queue.tf", "revision": self.base}]
            ),
            "excerpt must be 6-600 characters": self.base_claim(
                references=[
                    {"locator": "git:infra/queue.tf", "revision": self.base, "excerpt": "x" * 601}
                ]
            ),
            "references must be a list": self.base_claim(references="git:infra/queue.tf"),
            "reference must be an object": self.base_claim(references=["git:infra/queue.tf"]),
        }
        for expected, claim in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, "\n".join(cl.check_claim(claim)))
        self.assertEqual(cl.check_claim("C1"), ["claim must be an object"])
        self.assertEqual(cl.check_claims({}), ["claims must be a list"])
        self.assertIn(
            "duplicate claim id", cl.check_claims([self.base_claim(), self.base_claim()])[0]
        )

    def test_locators_are_parsed_or_refused(self):
        self.assertEqual(
            cl.parse_locator("git:a/b.tf#L2-L4"),
            {"kind": "git", "path": "a/b.tf", "start": 2, "end": 4},
        )
        self.assertEqual(cl.parse_locator("pr:42#comment-9001")["comment"], "9001")
        self.assertEqual(cl.parse_locator("doc:gdrive:1AbC_d-9")["source"], "gdrive")
        for bad in (
            "git:../secrets",
            "git:/etc/passwd",
            "git:a.tf#L5-L2",
            "git:-rf",
            "pr:42",
            "doc:Confluence:1",
            7,
        ):
            with self.subTest(bad), self.assertRaises(Refusal):
                cl.parse_locator(bad)


class Verification(ExplorerCase):
    def test_every_seeded_unsupported_claim_is_caught_and_no_supported_one_is(self):
        self.change()
        claims, expected = self.claims()
        report = self.run_cli(
            "claims", "check", "--file", self.write("claims.json", claims), expect=1
        )[0]
        verdict = {
            r["id"]: "not_asserting"
            if r["status"] == "unknown"
            else "supported"
            if r["supported"]
            else "unsupported"
            for r in report["results"]
        }
        self.assertEqual(verdict, expected)
        self.assertEqual(
            (report["claims"], report["asserting"], report["unsupported"]), (11, 10, 4)
        )
        self.assertEqual(report["unsupported_claim_rate"], 0.4)
        self.assertEqual(
            report["by_status"], {"confirmed": 8, "inferred": 1, "conflict": 1, "unknown": 1}
        )

    def test_each_failure_names_its_reason(self):
        self.change()
        claims, _ = self.claims()
        report = self.run_cli(
            "claims", "check", "--file", self.write("claims.json", claims), expect=1
        )[0]
        problems = {r["id"]: r["problems"] for r in report["results"] if r["problems"]}
        self.assertIn("excerpt_not_found", problems["C8"][0])
        self.assertIn("excerpt_not_found", problems["C9"][0])  # true at head, cited at base
        self.assertIn("excerpt_not_found", problems["C10"][0])  # right file, wrong lines
        self.assertIn("source_unavailable", problems["C11"][0])  # the document was never fetched

    def test_a_document_claim_holds_once_its_snapshot_is_saved(self):
        self.change()
        claims, _ = self.claims()
        runbook = [c for c in claims if c["id"] == "C11"]
        page = self.write("runbook.txt", "手順 4: 再配信で二重課金が起きた場合は手動で返金する。")
        saved = self.ok(
            "evidence",
            "add",
            "--locator",
            "doc:confluence:RUNBOOK-7",
            "--revision",
            "v3",
            "--file",
            page,
        )
        self.assertEqual(saved["revision"], "v3")
        report = self.ok("claims", "check", "--file", self.write("c11.json", runbook))
        self.assertEqual(report["unsupported"], 0)
        # another version of the same document is a different source
        runbook[0]["references"][0]["revision"] = "v4"
        self.run_cli("claims", "check", "--file", self.write("c11v4.json", runbook), expect=1)

    def test_lines_beyond_the_file_and_unknown_revisions_are_unsupported(self):
        claim = {
            "id": "C1",
            "status": "confirmed",
            "text": "x",
            "references": [
                {
                    "locator": "git:src/worker.py#L900",
                    "revision": self.base,
                    "excerpt": "ledger.charge",
                }
            ],
        }
        report = self.run_cli("claims", "check", "--file", self.write("c.json", [claim]), expect=1)[
            0
        ]
        self.assertIn("lines_out_of_range", report["results"][0]["problems"][0])
        claim["references"][0] = {
            "locator": "git:src/worker.py",
            "revision": "0" * 40,
            "excerpt": "ledger.charge",
        }
        report = self.run_cli(
            "claims", "check", "--file", self.write("c2.json", [claim]), expect=1
        )[0]
        self.assertIn("source_unavailable", report["results"][0]["problems"][0])

    def test_whitespace_differences_do_not_fail_an_excerpt(self):
        claim = {
            "id": "C1",
            "status": "confirmed",
            "text": "x",
            "references": [
                {
                    "locator": "git:infra/queue.tf",
                    "revision": self.base,
                    "excerpt": "message_retention_days = 7",
                }
            ],
        }
        self.assertEqual(
            self.ok("claims", "check", "--file", self.write("c.json", [claim]))["unsupported"], 0
        )

    def test_malformed_claims_are_refused_before_anything_is_read(self):
        refused = self.refused(
            "claims", "check", "--file", self.write("c.json", [{"id": "C1", "status": "sure"}])
        )
        self.assertIn("do not follow the contract", refused["error"])

    def test_no_asserting_claims_gives_no_rate(self):
        only_unknown = [{"id": "C1", "status": "unknown", "text": "理由", "searched": ["docs/"]}]
        self.assertIsNone(
            self.ok("claims", "check", "--file", self.write("c.json", only_unknown))[
                "unsupported_claim_rate"
            ]
        )


class Snapshots(ExplorerCase):
    def test_secrets_are_replaced_before_a_snapshot_is_stored(self):
        token = "ghp_" + "a1B2" * 9
        page = self.write("page.txt", f"deploy token: {token}\n手順は別紙")
        self.ok(
            "evidence",
            "add",
            "--locator",
            "doc:confluence:PAGE-1",
            "--revision",
            "v1",
            "--file",
            page,
        )
        stored = "".join(
            p.read_text(encoding="utf-8") for p in (self.tmp / "state").rglob("*.json")
        )
        self.assertNotIn(token, stored)
        self.assertIn("[REDACTED]", stored)

    def test_git_sources_and_malformed_references_are_not_snapshotted(self):
        page = self.write("page.txt", "x")
        git = self.refused(
            "evidence",
            "add",
            "--locator",
            "git:infra/queue.tf",
            "--revision",
            self.base,
            "--file",
            page,
        )
        self.assertIn("read from the repository", git["error"])
        bad = self.refused(
            "evidence",
            "add",
            "--locator",
            "doc:confluence:P",
            "--revision",
            "has space",
            "--file",
            page,
        )
        self.assertIn("revision must be a version label", bad["problems"][0])

    def test_an_oversized_snapshot_is_refused(self):
        page = self.write("big.txt", "x" * (2 * 1024 * 1024 + 1))
        refused = self.refused(
            "evidence", "add", "--locator", "doc:gdrive:BIG", "--revision", "1", "--file", page
        )
        self.assertIn("larger than", refused["error"])


class ReadOnlyGit(ExplorerCase):
    def test_only_reading_subcommands_run(self):
        for args in (
            ("commit", "-m", "x"),
            ("push",),
            ("checkout", "."),
            ("config", "user.name", "x"),
            (),
        ):
            with self.subTest(args), self.assertRaises(Refusal):
                gitread.git(self.repo, *args)

    def test_options_that_write_or_execute_are_refused(self):
        for option in ("--output=/tmp/x", "--ext-diff", "--textconv", "--exec=sh"):
            with self.subTest(option), self.assertRaises(Refusal):
                gitread.git(self.repo, "diff", option)

    def test_revisions_and_paths_cannot_smuggle_options(self):
        for revision in ("--output=x", "HEAD", "main; rm -rf", ""):
            with self.subTest(revision), self.assertRaises(Refusal):
                gitread.show(self.repo, revision, "infra/queue.tf")
        for path in ("-x", "a/../b", "/abs", "a\nb", "a:b", ""):
            with self.subTest(path), self.assertRaises(Refusal):
                gitread.show(self.repo, self.base, path)

    def test_reading_leaves_the_work_tree_and_refs_untouched(self):
        self.index_synthetic()
        self.change()
        before = synthetic_status(self.repo)
        self.ok("check")
        self.ok("packet", "--entity", "PROP-retention")
        self.ok("html")
        self.assertEqual(synthetic_status(self.repo), before)
        self.assertIsNone(gitread.diff(self.repo, "0" * 40, self.head))
        self.assertIn(
            "-  message_retention_days     = 7",
            gitread.diff(self.repo, self.base, self.head, "infra/queue.tf"),
        )

    def test_head_of_a_directory_without_commits_is_refused(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(Refusal):
            gitread.head(empty)


def synthetic_status(repo):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v2", "--branch", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


class ReviewFindings(ExplorerCase):
    """Regressions found by an independent review."""

    def test_a_snapshot_cannot_be_replaced_with_other_content(self):
        first = self.write("a.txt", "手順 4: 手動で返金する。")
        args = (
            "evidence",
            "add",
            "--locator",
            "doc:confluence:RUNBOOK-7",
            "--revision",
            "v3",
            "--file",
        )
        self.ok(*args, first)
        self.ok(*args, first)  # the same content again is not a change
        refused = self.refused(*args, self.write("b.txt", "手順 4: 返金しない。"))
        self.assertIn("already saved with other content", refused["error"])

    def test_unreadable_files_are_refusals(self):
        args = ("evidence", "add", "--locator", "doc:gdrive:X", "--revision", "1", "--file")
        self.assertIn("not found", self.refused(*args, str(self.tmp / "nope.txt"))["error"])
        binary = self.tmp / "bin.txt"
        binary.write_bytes(b"\xff\xfe\x00")
        self.assertIn("not UTF-8", self.refused(*args, str(binary))["error"])
        self.assertIn(
            "cannot be read", self.refused("index", "add", "--file", str(self.tmp))["error"]
        )

    def test_a_directory_is_not_a_source(self):
        claim = {
            "id": "C1",
            "status": "confirmed",
            "text": "x",
            "references": [
                {"locator": "git:infra", "revision": self.base, "excerpt": "queue.tf is here"}
            ],
        }
        report = self.run_cli("claims", "check", "--file", self.write("c.json", [claim]), expect=1)[
            0
        ]
        self.assertIn("source_unavailable", report["results"][0]["problems"][0])

    def test_an_excerpt_too_short_to_identify_anything_is_refused(self):
        claim = {
            "id": "C1",
            "status": "confirmed",
            "text": "x",
            "references": [
                {"locator": "git:infra/queue.tf", "revision": self.base, "excerpt": "="}
            ],
        }
        refused = self.refused("claims", "check", "--file", self.write("c.json", [claim]))
        self.assertIn("excerpt must be 6-600 characters", refused["problems"][0])

    def test_paths_with_invisible_or_bidi_characters_are_refused(self):
        for path in ("docs/a‮b.md", "docs/a​b.md", "docs/a⁦b.md"):
            with self.subTest(path), self.assertRaises(Refusal):
                gitread.checked_path(path)

    def test_a_torn_record_is_a_refusal_that_names_the_line(self):
        from context_recovery.base import read_jsonl

        log = self.tmp / "log.jsonl"
        log.write_text('{"event": "a"}\n{"event": "b", "x', encoding="utf-8")
        with self.assertRaises(Refusal) as raised:
            read_jsonl(log)
        self.assertIn("line 2", str(raised.exception))

    def test_a_repository_that_claims_to_be_a_partial_clone_cannot_start_a_program(self):
        import subprocess
        import sys

        ran = self.tmp / "ran"
        probe = self.tmp / "probe.py"
        probe.write_text(
            f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\n", encoding="utf-8"
        )
        for key, value in (
            ("core.repositoryformatversion", "1"),
            ("extensions.partialClone", "origin"),
            ("remote.origin.url", str(self.repo)),
            ("remote.origin.promisor", "true"),
            ("remote.origin.uploadpack", f"{sys.executable} {probe}"),
        ):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        missing = "1234567890123456789012345678901234567890"
        self.assertIsNone(gitread.resolve(self.repo, missing))
        self.assertIsNone(gitread.show(self.repo, missing, "infra/queue.tf"))
        self.assertIsNone(gitread.diff(self.repo, missing, self.base))
        self.assertFalse(ran.exists())
        self.assertIsNotNone(
            gitread.show(self.repo, self.base, "infra/queue.tf")
        )  # reading still works

    def test_a_lone_surrogate_in_input_is_a_refusal(self):
        entity = '[{"id": "DOC-1", "type": "document", "description": "x \\ud800 y"}]'
        refused = self.refused("index", "add", "--file", self.write("e.json", entity))
        self.assertIn("not valid Unicode text", refused["error"])

    def test_a_symlinked_scope_directory_is_refused(self):
        home = Path(self.ok("path")["home"])
        (home.parent / "planted").symlink_to(self.repo / "docs", target_is_directory=True)
        refused = self.run_cli("--scope", "planted", "path", expect=2)[1]
        self.assertIn("must not be a symlink", refused["error"])
        self.assertFalse((self.repo / "docs" / "index.json").exists())
