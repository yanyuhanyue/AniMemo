from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from release.acquisition import release_attestation_sidecar_name
from release.mirror import (
    MirrorError,
    build_offline_pair_mirror_plan,
    replicate_offline_pair_exact_bytes,
    replicate_offline_pair_files,
)
from release.portable import portable_release_asset_name

TAG = "v1.1.0-rc.TEST"
COMMIT = "a" * 40


def identity(value: bytes) -> dict[str, object]:
    return {
        "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
        "size": len(value),
    }


class PortableMirrorTests(unittest.TestCase):
    def test_filesystem_mirror_streams_the_closed_pair_without_repacking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            payload = b"portable payload"
            sidecar = b"github immutable release evidence"
            payload_name = portable_release_asset_name(TAG)
            sidecar_name = release_attestation_sidecar_name(TAG)
            (source / payload_name).write_bytes(payload)
            (source / sidecar_name).write_bytes(sidecar)
            plan = build_offline_pair_mirror_plan(
                authority="GITHUB_RELEASE",
                repository="yanyuhanyue/AniMemo",
                tag=TAG,
                commit=COMMIT,
                release_identity="sha256:" + "1" * 64,
                payload={"name": payload_name, **identity(payload)},
                release_attestation={"name": sidecar_name, **identity(sidecar)},
            )

            receipt = replicate_offline_pair_files(
                plan,
                source_directory=source,
                destination_directory=destination,
            )

            self.assertEqual((destination / payload_name).read_bytes(), payload)
            self.assertEqual((destination / sidecar_name).read_bytes(), sidecar)
            self.assertEqual(receipt["network_attempt"], 0)
            self.assertEqual(receipt["fallback_count"], 0)

    def test_release_cli_plans_and_executes_only_the_explicit_offline_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            payload_name = portable_release_asset_name(TAG)
            sidecar_name = release_attestation_sidecar_name(TAG)
            (source / payload_name).write_bytes(b"portable")
            (source / sidecar_name).write_bytes(b"attestation")
            plan = root / "mirror-plan.json"
            receipt = root / "mirror-receipt.json"
            repository_root = Path(__file__).resolve().parents[2]

            planned = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "release.cli",
                    "plan-offline-mirror",
                    "--repository",
                    "yanyuhanyue/AniMemo",
                    "--tag",
                    TAG,
                    "--commit",
                    COMMIT,
                    "--release-identity",
                    "sha256:" + "1" * 64,
                    "--payload",
                    str(source / payload_name),
                    "--release-attestation",
                    str(source / sidecar_name),
                    "--output",
                    str(plan),
                ],
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=False,
            )
            replicated = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "release.cli",
                    "replicate-offline-mirror",
                    "--plan",
                    str(plan),
                    "--source-directory",
                    str(source),
                    "--destination-directory",
                    str(destination),
                    "--output",
                    str(receipt),
                ],
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(replicated.returncode, 0, replicated.stderr)
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["role"], "TRANSPORT_ONLY")

    def test_mirror_copies_only_the_exact_dual_input_without_authority_or_fallback(self):
        payload = b"portable payload"
        sidecar = b"github immutable release evidence"
        payload_name = portable_release_asset_name(TAG)
        sidecar_name = release_attestation_sidecar_name(TAG)
        plan = build_offline_pair_mirror_plan(
            authority="GITHUB_RELEASE",
            repository="yanyuhanyue/AniMemo",
            tag=TAG,
            commit=COMMIT,
            release_identity="sha256:" + "1" * 64,
            payload={"name": payload_name, **identity(payload)},
            release_attestation={"name": sidecar_name, **identity(sidecar)},
        )
        mirrored: dict[str, bytes] = {}

        receipt = replicate_offline_pair_exact_bytes(
            plan,
            fetched={payload_name: payload, sidecar_name: sidecar},
            write=lambda name, value: mirrored.__setitem__(name, value),
            readback=mirrored.__getitem__,
        )

        self.assertEqual(mirrored, {payload_name: payload, sidecar_name: sidecar})
        self.assertEqual(receipt["role"], "TRANSPORT_ONLY")
        self.assertEqual(receipt["asset_count"], 2)
        self.assertEqual(plan["fallback_policy"], "FORBIDDEN")
        self.assertEqual(plan["version_selection"], "FORBIDDEN")

        with self.assertRaisesRegex(MirrorError, "missing or extra"):
            replicate_offline_pair_exact_bytes(
                plan,
                fetched={payload_name: payload, sidecar_name: sidecar, "extra": b"x"},
                write=lambda name, value: None,
                readback=lambda name: b"",
            )


if __name__ == "__main__":
    unittest.main()
