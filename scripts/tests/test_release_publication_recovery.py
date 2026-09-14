"""Exercise the real transaction CLI against isolated local journal/remote state."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.publication import build_publication_plan
from release.publication_remote import CommandResult, build_publication_runtime
from release.publication_transaction import (
    DurablePublicationController, LocalAtomicJournal, MutationResponse,
    PublicationTransactionError, RemoteObservation,
)
from release.test_publication_remote import _response
from scripts import release_publication as cli


def load_tests(loader, tests, pattern):
    # The existing scripts CI job must also exercise endpoint-specific discovery.
    tests.addTests(loader.loadTestsFromName("release.test_draft_discovery"))
    return tests


class AlreadyCommitted:
    def observe(self, intent):
        return RemoteObservation.same(intent.expected_identity)

    def mutate(self, intent):
        raise AssertionError("An already committed object must not be mutated")


class RecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.assets = self.root / "release-output"
        self.assets.mkdir()
        self.source = "1" * 40
        self.tree = "2" * 40
        self.tag = "v2.0.0-rc.1"
        self.writes = []
        self.release = None
        self.interrupt = None
        self.read_fail_once = False
        names = ("release-manifest.json", "deployment-contract.json", "installer-materials.tar")
        for name in names:
            (self.assets / name).write_bytes((name + "\n").encode())
        (self.assets / "checksums.txt").write_text("".join(
            hashlib.sha256((self.assets / name).read_bytes()).hexdigest() + "  " + name + "\n"
            for name in names
        ), encoding="utf-8")
        (self.assets / "release-notes.md").write_bytes(b"qualified notes\n")
        portable = f"animemo-{self.tag}-portable.tar"
        (self.assets / portable).write_bytes(b"fixture portable bytes")

        def identity(name):
            content = (self.assets / name).read_bytes()
            return {"sha256": "sha256:" + hashlib.sha256(content).hexdigest(), "size": len(content)}

        self.plan = build_publication_plan(
            repository="yanyuhanyue/AniMemo", channel="rc", tag=self.tag, commit=self.source,
            qualification_identity="sha256:" + "3" * 64,
            release_notes_identity="sha256:" + "4" * 64,
            release_notes_markdown_sha256=identity("release-notes.md")["sha256"],
            assets={name: identity(name) for name in (*names, "checksums.txt")},
            transport_assets={portable: {"role": "PORTABLE_RELEASE_BUNDLE", **identity(portable)}},
            api_digest="sha256:" + "5" * 64, web_digest="sha256:" + "6" * 64,
        )
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        self.runtime = build_publication_runtime(
            self.plan, source_tree=self.tree, asset_root=self.assets,
            candidate_root=self.root, repository_path=self.root,
            request=self.request, run=self.run_command,
        )
        # The fixture starts with all four registry keys, five attestations,
        # and the annotated tag present. They are forbidden write surfaces.
        for intent in self.runtime.intents:
            if intent.name.startswith(("registry-", "attestation-")) or intent.name == "git-tag":
                self.runtime.adapters[intent.name] = AlreadyCommitted()
        self.journal = LocalAtomicJournal(self.root / "journal")
        self.controller = self.open_controller()
        self.controller.preflight_all()

    def open_controller(self):
        return DurablePublicationController.open(
            self.plan, source_tree=self.tree, intents=self.runtime.intents,
            adapters=self.runtime.adapters, journal=self.journal,
        )

    def request(self, method, endpoint, payload):
        prefix = "repos/yanyuhanyue/AniMemo"
        if method == "GET":
            if self.read_fail_once:
                self.read_fail_once = False
                raise ConnectionError("fixture read failure")
            if endpoint == prefix:
                return _response({"permissions": {"push": True}})
            if endpoint == prefix + "/releases/tags/" + self.tag:
                return _response(self.release) if self.release and not self.release["draft"] else _response({}, 404)
            if endpoint == prefix + "/releases?per_page=100&page=1":
                return _response([self.release] if self.release else [])
            if endpoint == prefix + "/releases/999":
                return _response(self.release)
            raise AssertionError(endpoint)
        if method == "POST":
            self.assertIsNone(self.release, "Never create a second draft")
            self.release = {**payload, "id": 999, "assets": [], "immutable": False}
            self.writes.append("draft")
            if self.interrupt == "draft":
                raise KeyboardInterrupt()
            return _response(self.release, 201)
        self.assertEqual((method, endpoint), ("PATCH", prefix + "/releases/999"))
        self.assertEqual(len(self.release["assets"]), 5)
        self.release.update(payload, immutable=True)
        self.writes.append("publish")
        if self.interrupt == "publish":
            raise ConnectionError("fixture response lost after commit")
        return _response(self.release)

    def run_command(self, argv, timeout):
        self.assertEqual(argv[:3], ("gh", "release", "upload"))
        self.assertNotIn("--clobber", argv)
        path = Path(argv[4])
        self.assertFalse(any(a["name"] == path.name for a in self.release["assets"]))
        content = path.read_bytes()
        self.release["assets"].append({
            "name": path.name, "size": len(content), "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        })
        self.writes.append(path.name)
        if self.interrupt == "asset":
            raise KeyboardInterrupt()
        if self.interrupt == "asset-readback":
            self.read_fail_once = True
        return CommandResult(0, b"", b"")

    def command(self, name="transaction-run"):
        args = [name, "--plan", str(self.plan_path), "--source-tree", self.tree,
                "--asset-root", str(self.assets), "--candidate-root", str(self.root)]
        if name == "transaction-run":
            args += ["--phase", "publication"]
        else:
            args += ["--receipt", str(self.root / "receipt.json")]
        output, error = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli, "build_publication_runtime", return_value=self.runtime),
            mock.patch.object(cli, "GitRemoteAppendOnlyJournal", return_value=self.journal),
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(error),
        ):
            result = cli.main(args)
        return result, output.getvalue(), error.getvalue()

    def seed_ready_ack(self):
        # Reproduce the old published-only lookup on both sides of a real
        # adapter POST; all controller state transitions remain production code.
        with mock.patch.object(self.runtime.adapters["release-draft"], "_release", return_value=None):
            self.controller.advance("release-draft")
        step = next(s for s in self.controller.ledger["steps"] if s["name"] == "release-draft")
        self.assertEqual((step["state"], step["attempts"][0]["response"]["classification"]), ("READY", "ACKNOWLEDGED"))
        return copy.deepcopy(step["attempts"])

    def test_ready_ack_reconciles_then_five_assets_publish_and_finalize(self):
        attempts = self.seed_ready_ack()
        self.assertEqual(self.command()[0], 0)
        self.assertEqual(self.command("transaction-finalize")[0], 0)
        ledger = self.open_controller().ledger
        self.assertEqual(ledger["finalState"], "COMPLETE")
        self.assertEqual(len(ledger["steps"]), 17)
        step = next(s for s in ledger["steps"] if s["name"] == "release-draft")
        self.assertEqual(step["attempts"], attempts, "Historical ACK and erroneous ABSENT must remain")
        self.assertEqual(self.writes.count("draft"), 1)
        self.assertEqual(len(self.writes), 7)
        self.assertEqual(self.command()[0], 0)
        self.assertEqual(len(self.writes), 7)

    def test_post_interruption_and_asset_interruption_resume_by_observation(self):
        for stage in ("draft", "asset"):
            with self.subTest(stage=stage):
                # Separate fixtures retain each interrupted local journal.
                fixture = RecoveryCliTests()
                fixture.setUp()
                try:
                    fixture.interrupt = stage
                    with fixture.assertRaises(KeyboardInterrupt):
                        fixture.command()
                    before = list(fixture.writes)
                    fixture.interrupt = None
                    fixture.assertEqual(fixture.command()[0], 0)
                    for name in before:
                        fixture.assertEqual(fixture.writes.count(name), 1)
                    fixture.assertEqual(fixture.command("transaction-finalize")[0], 0)
                finally:
                    fixture.doCleanups()

    def test_publish_unknown_response_is_observed_without_second_patch(self):
        self.seed_ready_ack()
        self.interrupt = "publish"
        self.assertEqual(self.command()[0], 0)
        self.assertEqual(self.command("transaction-finalize")[0], 0)
        self.assertEqual(self.writes.count("publish"), 1)

    def test_asset_readback_unknown_freezes_and_reentry_cannot_overwrite(self):
        self.seed_ready_ack()
        self.interrupt = "asset-readback"
        result, _, error = self.command()
        self.assertEqual(result, 2)
        self.assertIn("TRANSACTION_GLOBAL_FREEZE", error)
        before = list(self.writes)
        self.interrupt = None
        self.assertEqual(self.command()[0], 2)
        self.assertEqual(self.writes, before)

    def test_damaged_journal_and_changed_plan_are_rejected(self):
        self.seed_ready_ack()
        self.plan["commit"] = "7" * 40
        with self.assertRaises((PublicationTransactionError, ValueError)):
            self.open_controller()
        ledger_path = sorted((self.journal.root / self.controller.ledger["operationId"][7:]).glob("*.json"))[-1]
        ledger_path.write_bytes(b'{"corrupted":true}')
        with self.assertRaises(PublicationTransactionError):
            self.journal.load(self.controller.ledger["operationId"])


if __name__ == "__main__":
    unittest.main()
