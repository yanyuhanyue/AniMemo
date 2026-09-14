"""Authenticate recovery records using fake API bytes and the real journal chain."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from release.candidate import canonical_json_bytes
from release.recovery_contract import RecoveryError, identity, policy
from release.recovery_evidence import validate_execution_record
from scripts.release_publication_controller import (
    ControllerReleaseAuthorityError,
    _GitHubReadOnlyObservationBoundary,
)
from scripts.tests import test_source_bound_recovery_replay as replay_tests
from scripts.tests.test_source_bound_recovery import execution_fixture


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return output.getvalue()


class RecoveryConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        capture = {}
        replay_tests.OriginalJournalReplayTests().replay(capture=capture)
        cls.capture = capture

    def setUp(self):
        self.p = policy()
        p = self.p
        self.boundary = object.__new__(_GitHubReadOnlyObservationBoundary)
        fixture = execution_fixture()
        self.run = {
            **fixture["run"],
            "status": "completed",
            "conclusion": "success",
            "name": fixture["run"]["display_title"],
        }
        self.record = {
            **copy.deepcopy(self.capture["execution"]),
            "status": "EVIDENCE_VERIFIED",
            "releaseId": p["transaction"]["draft_id"],
            "releaseTag": p["subject"]["release_tag"],
            "releasePublishedAt": "2026-09-14T12:00:30Z",
            "publicAssetBytes": {
                n: {"sha256": a["sha256"], "size": a["size"]}
                for n, a in {
                    **p["plan"]["assets"],
                    **p["plan"]["transport_assets"],
                }.items()
            },
            "originalProofs": replay_tests.SimulatedRemote.verify_proofs(None, None),
            "sidecarSha256": identity("sidecar"),
            "workflowConclusion": "READ_FROM_PLATFORM_AFTER_RUN_TERMINATES",
        }
        self.record["recordIdentity"] = identity(self.record)
        validate_execution_record(self.record)
        self.request = {
            "finalRepoHead": p["subject"]["sha"],
            "finalRepoTree": p["subject"]["tree"],
            "qualificationRunId": p["subject"]["qualification_run_id"],
            "candidateInputSha256": p["subject"]["candidate_input_sha256"],
            "verifiedCandidateIdentity": p["subject"]["verified_candidate_sha256"],
            "candidateAggregateReceiptSha256": p["subject"][
                "candidate_aggregate_sha256"
            ],
            "releaseTag": p["subject"]["release_tag"],
            "apiDigest": p["plan"]["api_digest"],
            "webDigest": p["plan"]["web_digest"],
        }
        self.raw = zipped({"execution-record.json": canonical_json_bytes(self.record)})
        self.artifact = {
            "id": 903,
            "name": "release-recovery-execution-v2.0.0-rc.1",
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(self.raw).hexdigest(),
            "size_in_bytes": len(self.raw),
            "workflow_run": {"id": self.run["id"], "head_sha": self.run["head_sha"]},
        }
        self.listing = {"total_count": 1, "artifacts": [self.artifact]}
        claim = self.record["claim"]
        base = "repos/" + p["repository"]
        self.api = {
            base + "/actions/workflows/release-recovery.yml": {"id": fixture["workflow"]["id"], "path": p["workflow"], "name": "Existing RC Recovery", "state": "active"},
            base + f"/pulls/{p['sourcePr']}": fixture["pull_request"],
            base + "/pulls/255": fixture["previous_pr"],
            base + "/git/commits/" + p["previousTool"]["sha"]: fixture[
                "previous_commit"
            ],
            base + "/git/commits/" + p["previousTool"]["reviewedHead"]: fixture[
                "previous_reviewed"
            ],
            base + f"/actions/runs/{p['supersededFailure']['runId']}": fixture[
                "superseded_run"
            ],
            base + "/git/commits/" + claim["toolSha"]: {
                "sha": claim["toolSha"],
                "tree": {"sha": claim["toolTree"]},
                "parents": [{"sha": p["previousTool"]["sha"]}],
            },
            base + "/git/commits/" + claim["reviewedHead"]: {
                "sha": claim["reviewedHead"],
                "tree": {"sha": claim["toolTree"]}
            },
            base
            + "/git/ref/heads/publication-transactions/"
            + p["transaction"]["operation_id"][7:]: {
                "object": {"sha": self.record["finalJournalHead"]}
            },
        }
        original = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "release/fixtures/rc-recovery-history.json"
            ).read_text(encoding="utf-8")
        )
        previous = None
        for ledger in self.capture["history"]:
            revision = ledger["revision"]
            head = (
                original[revision]["commit"]
                if revision <= 30
                else identity(ledger)[7:47]
            )
            raw = canonical_json_bytes(ledger)
            tree = identity(["tree", revision])[7:47]
            blob = identity(["blob", revision])[7:47]
            self.api[base + "/git/commits/" + head] = {
                "tree": {"sha": tree},
                "parents": [{"sha": previous}] if previous else [],
            }
            self.api[base + "/git/trees/" + tree] = {
                "truncated": False,
                "tree": [
                    {
                        "path": "ledger.json",
                        "type": "blob",
                        "mode": "100644",
                        "sha": blob,
                    }
                ],
            }
            self.api[base + "/git/blobs/" + blob] = {
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
            }
            previous = head

    def observe_record(self):
        with (
            mock.patch.object(
                self.boundary,
                "_run",
                side_effect=lambda cmd, **kw: (
                    (
                        b"HTTP/2.0 200 OK\nX-GitHub-Api-Version-Selected: 2022-11-28\n\n"
                        + json.dumps(self.api[cmd[-1]]).encode()
                    )
                    if "--include" in cmd
                    else self.raw
                ),
            ),
            mock.patch.object(
                self.boundary,
                "_gh_json",
                side_effect=lambda endpoint: copy.deepcopy(self.api[endpoint]),
            ),
        ):
            return self.boundary._recovery_record(self.listing, self.run, self.request)

    def test_original_product_new_executor_and_complete_real_chain(self):
        record = self.observe_record()
        self.assertNotEqual(record["claim"]["toolSha"], self.request["finalRepoHead"])
        self.assertEqual(record["finalRevision"], 46)
        self.assertEqual(
            {x["source"] for x in record["originalProofs"]},
            {self.request["finalRepoHead"]},
        )

    def test_authenticated_run_artifact_pr_and_journal_mismatches(self):
        claim = self.record["claim"]
        base = "repos/" + self.p["repository"]
        cases = [
            (self.artifact, "digest", "sha256:" + "0" * 64),
            (self.run, "id", 904),
            (
                self.api[base + "/git/commits/" + claim["reviewedHead"]],
                "tree",
                {"sha": "f" * 40},
            ),
            (
                self.api[
                    base
                    + "/git/ref/heads/publication-transactions/"
                    + self.p["transaction"]["operation_id"][7:]
                ],
                "object",
                {"sha": "f" * 40},
            ),
            (self.request, "finalRepoHead", claim["toolSha"]),
        ]
        for obj, key, value in cases:
            previous = copy.deepcopy(obj[key])
            obj[key] = value
            with (
                self.subTest(key=key),
                self.assertRaises(ControllerReleaseAuthorityError),
            ):
                self.observe_record()
            obj[key] = previous

    def test_record_rejects_replaced_signer_duplicates_and_extra_writes(self):
        for change in ("signer", "duplicate", "write", "time"):
            record = copy.deepcopy(self.record)
            if change == "signer":
                record["originalProofs"][0]["source"] = record["claim"]["toolSha"]
            if change == "duplicate":
                record["originalProofs"][0] = record["originalProofs"][1]
            if change == "write":
                record["writeRequests"].append(
                    {"method": "POST", "kind": "DRAFT", "draftId": 388147631}
                )
            if change == "time":
                record["completedAt"] = "2026-09-15T12:00:00Z"
            record["recordIdentity"] = identity(
                {k: v for k, v in record.items() if k != "recordIdentity"}
            )
            with self.subTest(change=change), self.assertRaises(RecoveryError):
                validate_execution_record(record)

    def test_metadata_archive_uses_api_digest_and_closed_member_set(self):
        from hashlib import sha256

        for files, tamper in (
            ({"release-mirror.json": b"{}"}, False),
            ({"release-mirror.json": b"{}"}, True),
            ({"../release-mirror.json": b"{}"}, False),
        ):
            raw = zipped(files)
            artifact = {
                "id": 904,
                "size_in_bytes": len(raw),
                "digest": "sha256:" + sha256(raw).hexdigest(),
            }
            if tamper:
                artifact["digest"] = "sha256:" + "0" * 64
            with (
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(self.boundary, "_run", return_value=raw),
            ):
                if tamper or "../release-mirror.json" in files:
                    with self.assertRaises(ControllerReleaseAuthorityError):
                        self.boundary._download_recovery_metadata(
                            artifact, Path(directory), {"release-mirror.json"}
                        )
                else:
                    self.boundary._download_recovery_metadata(
                        artifact, Path(directory), {"release-mirror.json"}
                    )
                    self.assertEqual(
                        (Path(directory) / "release-mirror.json").read_bytes(), b"{}"
                    )

    def test_manual_mirror_requires_new_executor_and_attempt_one(self):
        run = {
            **self.run,
            "id": 905,
            "name": "Release Mirror",
            "path": ".github/workflows/release-mirror.yml",
        }
        args = {
            "run_id": 905,
            "name": "Release Mirror",
            "path": run["path"],
            "head": self.run["head_sha"],
            "events": frozenset({"workflow_dispatch"}),
            "head_branches": frozenset({"main"}),
        }
        self.boundary._validate_run(run, **args)
        for update in (
            {"event": "release"},
            {"head_sha": self.p["subject"]["sha"]},
            {"run_attempt": 2},
            {"head_branch": "v2.0.0-rc.1"},
        ):
            with (
                self.subTest(update=update),
                self.assertRaises(ControllerReleaseAuthorityError),
            ):
                self.boundary._validate_run({**run, **update}, **args)

    def test_observe_orchestrates_recovery_and_binds_actual_public_objects(self):
        # Existing format/crypto validators have their own suites. Here their
        # deterministic outputs isolate the new authenticated orchestration.
        from contextlib import ExitStack
        from types import SimpleNamespace

        from scripts import release_publication_controller as module

        p = self.p
        base = "repos/" + p["repository"]
        tag = p["subject"]["release_tag"]
        self.request.update(
            publishRunId=901,
            mirrorRunId=905,
            releaseVersion=tag,
            releaseChannel="rc",
            publicationIdentity=identity("publication"),
        )
        mirror_run = {
            **self.run,
            "id": 905,
            "name": "Release Mirror",
            "path": ".github/workflows/release-mirror.yml",
        }
        self.api[base + "/actions/runs/901"] = self.run
        self.api[base + "/actions/runs/905"] = mirror_run
        self.api[base + "/git/commits/" + p["subject"]["sha"]] = {
            "tree": {"sha": p["subject"]["tree"]}
        }
        for rid, name in ((901, "recover-existing-rc"), (905, "发布不可变发行镜像")):
            self.api[base + f"/actions/runs/{rid}/jobs?per_page=100"] = {
                "total_count": 1,
                "jobs": [
                    {"name": name, "status": "completed", "conclusion": "success"}
                ],
            }
        sidecar = f"animemo-{tag}-release-attestation.json"
        files = {name: b"{}" for name in module._RELEASE_METADATA_FILES}
        files[sidecar] = canonical_json_bytes("sidecar")
        metadata_raw = zipped(files)
        mirror_raw = zipped({"release-mirror.json": b"{}"})

        def artifact(aid, name, raw, rid):
            return {
                "id": aid,
                "name": name,
                "expired": False,
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size_in_bytes": len(raw),
                "workflow_run": {"id": rid, "head_sha": self.run["head_sha"]},
            }

        self.api[base + "/actions/runs/901/artifacts?per_page=100"] = {
            "total_count": 2,
            "artifacts": [
                self.artifact,
                artifact(904, "release-publication-metadata-" + tag, metadata_raw, 901),
            ],
        }
        self.api[base + "/actions/runs/905/artifacts?per_page=100"] = {
            "total_count": 1,
            "artifacts": [artifact(906, "release-mirror-388147631", mirror_raw, 905)],
        }
        downloads = {903: self.raw, 904: metadata_raw, 906: mirror_raw}
        declared = self.record["publicAssetBytes"]
        candidate = {
            key: declared[name]["sha256"]
            for key, name in [
                ("checksums_sha256", "checksums.txt"),
                ("deployment_contract_sha256", "deployment-contract.json"),
                ("installer_materials_sha256", "installer-materials.tar"),
                ("release_manifest_sha256", "release-manifest.json"),
            ]
        }
        candidate.update(
            api_oci_digest=p["plan"]["api_digest"],
            web_oci_digest=p["plan"]["web_digest"],
        )
        actual = {"releaseId": 388147631, "tagObject": p["tagObject"]}

        def public(*, root, **_kwargs):
            (root / "release-manifest.json").write_bytes(
                canonical_json_bytes(
                    {
                        "release": {"version": tag, "commit": p["subject"]["sha"]},
                        "images": {
                            "api": {"digest": p["plan"]["api_digest"]},
                            "web": {"digest": p["plan"]["web_digest"]},
                        },
                        "deployment": {"contractSha256": identity("contract")},
                    }
                )
            )
            (root / "deployment-contract.json").write_bytes(b"{}")
            return actual["releaseId"], actual["tagObject"], declared

        portable = declared[f"animemo-{tag}-portable.tar"]
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    self.boundary,
                    "_gh_json",
                    side_effect=lambda e: copy.deepcopy(self.api[e]),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.boundary,
                    "_run",
                    side_effect=lambda command, **_k: (
                        (
                            b"HTTP/2.0 200 OK\nX-GitHub-Api-Version-Selected: 2022-11-28\n\n"
                            + json.dumps(self.api[command[-1]]).encode()
                        )
                        if "--include" in command
                        else downloads[int(command[-1].split("/")[-2])]
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.boundary,
                    "_verify_metadata",
                    return_value=(candidate, p["plan"], self.capture["ledger"]),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.boundary, "_verify_public_release", side_effect=public
                )
            )
            proofs = stack.enter_context(
                mock.patch.object(self.boundary, "_verify_attestations")
            )
            stack.enter_context(mock.patch.object(self.boundary, "_verify_registry"))
            mirror = stack.enter_context(
                mock.patch.object(self.boundary, "_verify_mirror")
            )
            for name in (
                "_verify_checksums",
                "validate_manifest",
                "validate_deployment_contract",
            ):
                stack.enter_context(mock.patch.object(module, name))
            stack.enter_context(
                mock.patch.object(
                    module,
                    "deployment_contract_digest",
                    return_value=identity("contract"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    module,
                    "inspect_portable_archive",
                    return_value=SimpleNamespace(
                        archive_sha256=portable["sha256"], archive_size=portable["size"]
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    module,
                    "close_github_release_publication",
                    return_value=SimpleNamespace(
                        identity=self.request["publicationIdentity"]
                    ),
                )
            )
            arguments = {
                "authority_request": self.request,
                "expected_public_identity": {},
                "candidate_result": {},
            }
            result = self.boundary.observe(**arguments)
            self.assertEqual(result.final_repo_head, p["subject"]["sha"])
            self.assertEqual(
                proofs.call_args.kwargs["request"]["finalRepoHead"], p["subject"]["sha"]
            )
            mirror.assert_called_once()
            for field, value in (("releaseId", 373784357), ("tagObject", "f" * 40)):
                old = actual[field]
                actual[field] = value
                with (
                    self.subTest(field=field),
                    self.assertRaises(ControllerReleaseAuthorityError),
                ):
                    self.boundary.observe(**arguments)
                actual[field] = old
            altered = zipped({**files, sidecar: b"changed but separately verified"})
            downloads[904] = altered
            self.api[base + "/actions/runs/901/artifacts?per_page=100"]["artifacts"][
                1
            ] = artifact(904, "release-publication-metadata-" + tag, altered, 901)
            with self.assertRaises(ControllerReleaseAuthorityError):
                self.boundary.observe(**arguments)


if __name__ == "__main__":
    unittest.main()
