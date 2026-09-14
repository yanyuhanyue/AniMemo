"""Original 31 snapshots through the production engine with isolated transports.

Set ANIMEMO_RECOVERY_REPLAY_REAL_BYTES=1 to use this task's prepare_materials result for
the full original-byte replay. CI exercises the same controller and journal
using transport stubs; it never has a recovery credential or remote endpoint.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.publication_transaction import LocalAtomicJournal, MutationResponse
from release.recovery import ExistingRCRecovery
from release.recovery_contract import (
    RecoveryError,
    identity,
    policy,
    validate_execution,
)
from release.recovery_remote import file_digest
from scripts.tests.test_source_bound_recovery import execution_fixture


class IsolatedJournal:
    def __init__(self, root):
        self.local = LocalAtomicJournal(root)
        self.history = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "release/fixtures/rc-recovery-history.json"
            ).read_text(encoding="utf-8")
        )
        for item in self.history:
            self.local.append(json.loads(item["raw"]))
        self.head = self.history[-1]["commit"]
        self.last_written_head = None

    def _remote_head(self, operation):
        assert operation == policy()["transaction"]["operation_id"]
        return self.head

    def load(self, operation):
        return self.local.load(operation)

    def append(self, value):
        accepted = self.local.append(value)
        self.head = identity(accepted)[7:47]
        self.last_written_head = self.head
        return accepted


class SimulatedRemote:
    """Only transport outcomes are simulated; no canonical gate is replaced."""

    base = "repos/yanyuhanyue/AniMemo"

    def __init__(self, assets, *, real_bytes, outcome="ack", published=False):
        self.assets, self.real_bytes, self.outcome = assets, real_bytes, outcome
        self.write_requests = []
        p = policy()
        self.release = {
            "id": p["transaction"]["draft_id"],
            "tag_name": p["subject"]["release_tag"],
            "name": p["subject"]["release_tag"],
            "body": (assets / "release-notes.md").read_text(encoding="utf-8"),
            "target_commitish": "main",
            "draft": not published,
            "prerelease": True,
            "immutable": published,
            "assets": [],
        }

    def draft(self, *, published_allowed=False):
        if self.release["draft"] is False and not published_allowed:
            raise RecoveryError("RECOVERY_DRAFT_STATE_INVALID")
        return copy.deepcopy(self.release)

    def listed(self, endpoint, key):
        assert key == "artifacts"
        rows = [
            {**item, "expired": False}
            for item in policy()["qualificationArtifacts"].values()
        ]
        return {"total_count": len(rows), "artifacts": rows}

    def verify_proofs(self, _root):
        return [
            {
                "name": name,
                "verifiedProofDigest": identity(name),
                "source": policy()["subject"]["sha"],
                "signer": ".github/workflows/release.yml",
            }
            for name in (
                "api",
                "web",
                "release-manifest.json",
                "deployment-contract.json",
                "installer-materials.tar",
            )
        ]

    def upload(self, path):
        declared = {
            **policy()["plan"]["assets"],
            **policy()["plan"]["transport_assets"],
        }[path.name]
        if self.real_bytes:
            assert file_digest(path) == (declared["sha256"], declared["size"])
        self.write_requests.append(
            {
                "method": "POST",
                "kind": "ASSET",
                "name": path.name,
                "draftId": self.release["id"],
            }
        )
        if self.outcome != "missing":
            self.release["assets"].append(
                {
                    "id": 100 + len(self.write_requests),
                    "name": path.name,
                    "size": declared["size"],
                    "digest": declared["sha256"],
                    "state": "uploaded",
                    "url": "https://api.github.com/fixture",
                }
            )
        return (
            MutationResponse.acknowledged()
            if self.outcome == "ack"
            else MutationResponse.ambiguous("SIMULATED_RESPONSE_LOSS")
        )

    def publish(self):
        assert len(self.release["assets"]) == 5
        self.write_requests.append(
            {"method": "PATCH", "kind": "PUBLISH", "draftId": self.release["id"]}
        )
        self.release.update(
            draft=False, immutable=True, published_at="2026-09-14T12:00:30Z"
        )
        return (
            MutationResponse.ambiguous("SIMULATED_RESPONSE_LOSS")
            if self.outcome == "lost"
            else MutationResponse.acknowledged()
        )


class SimulatedPlatform:
    def __init__(self, remote, root):
        self.remote, self.repository = remote, root
        self.fixture = execution_fixture()
        self.claim = validate_execution(**self.fixture)

    def now(self):
        return self.fixture["now"]

    def execution(self):
        return copy.deepcopy(self.claim)

    def validate_active(self, claim):
        assert claim == self.claim
        return self.now()

    def validate_settings(self):
        pass

    def tag(self):
        return {"object": policy()["tagObject"], "commit": policy()["subject"]["sha"]}

    def registry(self):
        return {
            s["name"]: s["expectedIdentity"]
            for s in policy()["steps"]
            if s["name"].startswith("registry-")
        }


class OriginalJournalReplayTests(unittest.TestCase):
    def replay(self, outcome="ack", real_materials=None, capture=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = IsolatedJournal(root / "journal")
            if real_materials:
                materials = json.loads(Path(real_materials).read_text(encoding="utf-8"))
                workspace = Path(materials["assetRoot"]).parent
                assets = Path(materials["assetRoot"])
            else:
                workspace = root
                assets = root / "release-output"
                assets.mkdir()
                p = policy()
                # Only runtime's fixed locator construction needs these files.
                # Bulk byte verification is transport-stubbed in this fast test.
                notes_path = (
                    Path(__file__).resolve().parents[2]
                    / "release/fixtures/rc-recovery-notes.md"
                )
                (assets / "release-notes.md").write_bytes(notes_path.read_bytes())
                for name in {**p["plan"]["assets"], **p["plan"]["transport_assets"]}:
                    (assets / name).write_bytes(b"fixture")
                (assets / "checksums.txt").write_text(
                    "".join(
                        p["plan"]["assets"][n]["sha256"][7:] + "  " + n + "\n"
                        for n in (
                            "release-manifest.json",
                            "deployment-contract.json",
                            "installer-materials.tar",
                        )
                    ),
                    encoding="utf-8",
                )
                materials = {
                    "plan": p["plan"],
                    "candidateRoot": str(root),
                    "assetRoot": str(assets),
                    "materialBinding": identity(p["plan"]),
                }
            remote = SimulatedRemote(
                assets, real_bytes=bool(real_materials), outcome=outcome
            )
            platform = SimulatedPlatform(remote, root)
            engine = ExistingRCRecovery(
                platform=platform, backend=backend, materials=materials, root=root
            )
            original = backend.load(platform.claim["operationId"])
            before = {
                path.name: path.read_bytes()
                for path in backend.local._directory(
                    platform.claim["operationId"]
                ).glob("*.json")
            }
            old_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                if not real_materials:
                    # Unit-level bulk observations; production controller,
                    # runtime, adapters, guard and all 31 snapshots remain real.
                    with mock.patch.object(
                        engine.precheck, "snapshot", return_value={"fixture": True}
                    ):
                        if outcome == "missing":
                            with self.assertRaises(RecoveryError):
                                engine.run()
                            self.assertEqual(len(remote.write_requests), 1)
                            return
                        result = engine.run()
                else:
                    result = engine.run()
            finally:
                os.chdir(old_cwd)
            self.assertEqual(result["status"], "TRANSACTION_COMPLETE")
            self.assertEqual(result["finalRevision"], 46)
            self.assertEqual(result["journalAppendCount"], 16)
            self.assertEqual(len(remote.write_requests), 6)
            self.assertEqual(
                [x["name"] for x in remote.write_requests[:-1]],
                [
                    s["remoteKey"].split(":asset:")[1]
                    for s in policy()["steps"]
                    if s["name"].startswith("release-asset-")
                ],
            )
            final = backend.load(platform.claim["operationId"])
            if capture is not None:
                capture.update(
                    execution=result,
                    ledger=final,
                    history=[
                        json.loads(path.read_bytes())
                        for path in sorted(
                            backend.local._directory(
                                platform.claim["operationId"]
                            ).glob("*.json")
                        )
                    ],
                )
            for old, new in zip(original["steps"], final["steps"]):
                self.assertEqual(
                    old["attempts"], new["attempts"][: len(old["attempts"])]
                )
            for name, raw in before.items():
                self.assertEqual(
                    (
                        backend.local._directory(platform.claim["operationId"]) / name
                    ).read_bytes(),
                    raw,
                )
            with self.assertRaises(RecoveryError):
                ExistingRCRecovery(
                    platform=platform, backend=backend, materials=materials, root=root
                ).run()
            return result

    def test_complete_original_history_through_new_engine(self):
        self.replay()

    def test_lost_asset_and_publish_responses_reconcile_without_repetition(self):
        self.replay("lost")

    def test_unknown_absent_after_upload_stops_without_second_request(self):
        self.replay("missing")

    @unittest.skipUnless(
        os.environ.get("ANIMEMO_RECOVERY_REPLAY_REAL_BYTES") == "1",
        "Full original asset bytes are a separate local acceptance input",
    )
    def test_full_original_bytes_and_live_precheck(self):
        material_result = (
            Path(__file__).resolve().parents[4]
            / ".animemo-audit-work"
            / "rc-source-bound-recovery-20260914-v1"
            / "material-replay-final-result.json"
        )
        self.replay(real_materials=material_result)


if __name__ == "__main__":
    unittest.main()
