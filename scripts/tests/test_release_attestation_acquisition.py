from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release import acquisition as acquisition_module
from release.acquisition import (
    REQUIRED_ACTIONS_EVIDENCE,
    AttestationAcquisitionError,
    GitHubAttestationAcquirer,
    validate_attestation_sidecar,
)
from release.materials import bound_release_directory_io_available
from release.portable import portable_release_asset_name

REPOSITORY = "yanyuhanyue/AniMemo"
TAG = "v1.1.0-rc.TEST"
COMMIT = "a" * 40
WORKFLOW = ".github/workflows/release.yml"


class ReleaseAttestationAcquisitionTests(unittest.TestCase):
    @unittest.skipUnless(
        bound_release_directory_io_available(),
        "descriptor-relative directory binding unavailable",
    )
    def test_exclusive_export_rejects_parent_path_rebind_without_off_tree_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "bound" / "output"
            parent.mkdir(parents=True)
            detached = root / "detached"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_bytes(b"unchanged")
            real_open_bound_directory = acquisition_module.open_bound_release_directory

            def rebind_after_open(path):
                descriptor = real_open_bound_directory(path)
                parent.rename(detached)
                parent.symlink_to(outside, target_is_directory=True)
                return descriptor

            with (
                mock.patch.object(
                    acquisition_module,
                    "open_bound_release_directory",
                    side_effect=rebind_after_open,
                ),
                self.assertRaisesRegex(
                    AttestationAcquisitionError,
                    "SIDECAR_DESTINATION_REBOUND",
                ),
            ):
                acquisition_module._write_exclusive(parent / "sidecar.json", b"value")

            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertFalse((outside / "sidecar.json").exists())
            self.assertFalse((detached / "sidecar.json").exists())

    def _subjects(self, root: Path) -> dict[str, str]:
        values: dict[str, str] = {
            "api-image": "oci://ghcr.io/yanyuhanyue/animemo-api@sha256:"
            + "1" * 64,
            "web-image": "oci://ghcr.io/yanyuhanyue/animemo-web@sha256:"
            + "2" * 64,
        }
        for name in ("release-manifest", "deployment-contract", "installer-materials"):
            path = root / name
            path.write_bytes((name + "\n").encode("ascii"))
            values[name] = str(path)
        return values

    @unittest.skipUnless(
        bound_release_directory_io_available(),
        "descriptor-relative directory binding unavailable",
    )
    def test_exact_tag_acquisition_exports_deterministic_dual_input_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / portable_release_asset_name(TAG)
            payload.write_bytes(b"deterministic portable bytes")
            commands: list[tuple[str, ...]] = []

            def runner(command: tuple[str, ...]) -> bytes:
                commands.append(command)
                return json.dumps(
                    [{"attestation": {"command": list(command)}}],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")

            acquirer = GitHubAttestationAcquirer(runner=runner)
            first = root / "first.json"
            second = root / "second.json"
            subjects = self._subjects(root)
            acquirer.acquire_and_export(
                repository=REPOSITORY,
                tag=TAG,
                commit=COMMIT,
                workflow=WORKFLOW,
                payload=payload,
                actions_subjects=subjects,
                destination=first,
                actions_source_commits={"release-manifest": "b" * 40},
            )
            acquirer.acquire_and_export(
                repository=REPOSITORY,
                tag=TAG,
                commit=COMMIT,
                workflow=WORKFLOW,
                payload=payload,
                actions_subjects=subjects,
                destination=second,
                actions_source_commits={"release-manifest": "b" * 40},
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            envelope = validate_attestation_sidecar(first.read_bytes(), payload=payload)
            self.assertEqual(
                envelope["payload"]["sha256"],
                "sha256:" + hashlib.sha256(payload.read_bytes()).hexdigest(),
            )
            self.assertEqual(set(envelope["evidence"]), {"github-release", "portable-asset", *REQUIRED_ACTIONS_EVIDENCE})
            self.assertEqual(commands[0][0:4], ("gh", "release", "verify", TAG))
            self.assertEqual(commands[1][0:4], ("gh", "release", "verify-asset", TAG))
            self.assertNotIn("latest", " ".join(word for command in commands for word in command).lower())
            for command in (
                item for item in commands if item[0:3] == ("gh", "attestation", "verify")
            ):
                self.assertIn(f"{REPOSITORY}/{WORKFLOW}", command)
                expected_commit = (
                    "b" * 40
                    if str(subjects["release-manifest"]) in command
                    else COMMIT
                )
                self.assertIn(expected_commit, command)

            payload.write_bytes(b"changed")
            with self.assertRaisesRegex(
                AttestationAcquisitionError, "PAYLOAD_IDENTITY_MISMATCH"
            ):
                validate_attestation_sidecar(first.read_bytes(), payload=payload)

    @unittest.skipIf(
        bound_release_directory_io_available(),
        "descriptor-relative directory binding is available",
    )
    def test_export_fails_closed_without_descriptor_relative_io(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sidecar.json"
            with self.assertRaisesRegex(
                AttestationAcquisitionError,
                "SIDECAR_DESCRIPTOR_RELATIVE_IO_REQUIRED",
            ):
                acquisition_module._write_exclusive(destination, b"value")
            self.assertFalse(destination.exists())

    def test_missing_actions_subject_is_rejected_before_external_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / portable_release_asset_name(TAG)
            payload.write_bytes(b"portable")
            calls: list[tuple[str, ...]] = []
            acquirer = GitHubAttestationAcquirer(
                runner=lambda command: calls.append(command) or b"[]"
            )
            subjects = self._subjects(root)
            subjects.pop("api-image")

            with self.assertRaisesRegex(
                AttestationAcquisitionError, "ACTIONS_SUBJECT_SET_INVALID"
            ):
                acquirer.acquire_and_export(
                    repository=REPOSITORY,
                    tag=TAG,
                    commit=COMMIT,
                    workflow=WORKFLOW,
                    payload=payload,
                    actions_subjects=subjects,
                    destination=root / "sidecar.json",
                )

            self.assertEqual(calls, [])

    def test_invalid_actions_source_commit_is_rejected_before_external_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / portable_release_asset_name(TAG)
            payload.write_bytes(b"portable")
            calls: list[tuple[str, ...]] = []

            with self.assertRaisesRegex(
                AttestationAcquisitionError, "ACTIONS_SOURCE_COMMIT_SET_INVALID"
            ):
                GitHubAttestationAcquirer(
                    runner=lambda command: calls.append(command) or b"[]"
                ).acquire_and_export(
                    repository=REPOSITORY,
                    tag=TAG,
                    commit=COMMIT,
                    workflow=WORKFLOW,
                    payload=payload,
                    actions_subjects=self._subjects(root),
                    destination=root / "sidecar.json",
                    actions_source_commits={"release-manifest": "latest"},
                )

            self.assertEqual(calls, [])

    def test_empty_actions_subject_is_rejected_before_external_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / portable_release_asset_name(TAG)
            payload.write_bytes(b"portable")
            calls: list[tuple[str, ...]] = []
            acquirer = GitHubAttestationAcquirer(
                runner=lambda command: calls.append(command) or b"[]"
            )
            subjects = self._subjects(root)
            subjects["api-image"] = ""

            with self.assertRaisesRegex(
                AttestationAcquisitionError, "ACTIONS_SUBJECT_INVALID:api-image"
            ):
                acquirer.acquire_and_export(
                    repository=REPOSITORY,
                    tag=TAG,
                    commit=COMMIT,
                    workflow=WORKFLOW,
                    payload=payload,
                    actions_subjects=subjects,
                    destination=root / "sidecar.json",
                )

            self.assertEqual(calls, [])

    def test_ambiguous_verified_bundle_set_stops_at_first_online_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / portable_release_asset_name(TAG)
            payload.write_bytes(b"portable")
            calls: list[tuple[str, ...]] = []

            def runner(command: tuple[str, ...]) -> bytes:
                calls.append(command)
                return json.dumps(
                    [
                        {"attestation": {"candidate": 1}},
                        {"attestation": {"candidate": 2}},
                    ]
                ).encode("utf-8")

            with self.assertRaisesRegex(
                AttestationAcquisitionError,
                "ATTESTATION_VERIFICATION_OUTPUT_INVALID:github-release",
            ):
                GitHubAttestationAcquirer(runner=runner).acquire_and_export(
                    repository=REPOSITORY,
                    tag=TAG,
                    commit=COMMIT,
                    workflow=WORKFLOW,
                    payload=payload,
                    actions_subjects=self._subjects(root),
                    destination=root / "sidecar.json",
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0:4], ("gh", "release", "verify", TAG))

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_unsafe_payload_is_rejected_before_any_online_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / portable_release_asset_name(TAG)
            payload.write_bytes(b"portable")
            os.link(payload, root / "second-link")
            calls: list[tuple[str, ...]] = []
            acquirer = GitHubAttestationAcquirer(
                runner=lambda command: calls.append(command) or b"[]"
            )

            with self.assertRaisesRegex(
                AttestationAcquisitionError, "PAYLOAD_PATH_UNSAFE"
            ):
                acquirer.acquire_and_export(
                    repository=REPOSITORY,
                    tag=TAG,
                    commit=COMMIT,
                    workflow=WORKFLOW,
                    payload=payload,
                    actions_subjects=self._subjects(root),
                    destination=root / "sidecar.json",
                )

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
