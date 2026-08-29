from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release import trust_bootstrap
from release.materials import build_installer_materials
from release.trust_bootstrap import (
    TUFMetadataNotFound,
    build_initial_trust_kit,
)
from updater.offline import TrustProfile


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class InitialTrustBootstrapTests(unittest.TestCase):
    def test_verifier_replacement_during_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = root / "offline-release-verifier"
            replacement = root / "replacement-verifier"
            verifier.write_bytes(b"qualified verifier")
            replacement.write_bytes(b"malicious verifier")
            real_open = os.open

            with (
                mock.patch.object(
                    trust_bootstrap.os,
                    "open",
                    side_effect=lambda _path, flags: real_open(replacement, flags),
                ),
                self.assertRaisesRegex(
                    trust_bootstrap.TrustBootstrapError,
                    "读取期间发生变化",
                ),
            ):
                trust_bootstrap._read_verifier(verifier)

    def test_versioned_installation_contract_closes_the_v2_lifecycle(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[2]
            / "release"
            / "release_attestation_verifier"
            / "INSTALLATION_CONTRACT_V2.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual(contract["schemaVersion"], 2)
        self.assertEqual(
            contract["contract"]["predecessorBlob"],
            "a6307b42928423cea3a2cf04db7836887fc818a0",
        )
        self.assertEqual(
            contract["contract"]["freezeStatus"],
            "FROZEN_FOR_V1_1_P1_SECURITY_REPAIR",
        )
        self.assertEqual(contract["bootstrap"]["bundleSelfAuthorization"], "FORBIDDEN")
        self.assertEqual(len(contract["generationFiles"]), 6)
        self.assertEqual(
            contract["state"]["generationRoot"],
            "/var/lib/animemo/offline-trust/v2/generations",
        )
        self.assertEqual(
            contract["trustUpdate"]["authorityRole"],
            "TRUST_METADATA_ONLY",
        )
        self.assertFalse(contract["trustUpdate"]["supersessionIsRevocation"])
        successor = Path(__file__).resolve().parents[2] / "docs" / "installer-contract-v2.md"
        successor_text = successor.read_text(encoding="utf-8")
        self.assertIn("FROZEN FOR v1.1 P1 SECURITY REPAIR", successor_text)
        self.assertIn("PYTHONSAFEPATH=1", successor_text)
        self.assertIn("SUPERSEDED`, not automatically", successor_text)

    def test_two_official_tuf_tracks_produce_closed_pretrust_kit(self) -> None:
        roots = {
            "github": b'{"signed":{"version":3},"signatures":[]}\n',
            "sigstore": b'{"signed":{"version":14},"signatures":[]}\n',
        }
        successors = {
            "github": b'{"signed":{"version":4},"signatures":[]}\n',
            "sigstore": b'{"signed":{"version":15},"signatures":[]}\n',
        }
        trusted = {
            "github": b'{"mediaType":"github-trusted-root"}\n',
            "sigstore": b'{"mediaType":"sigstore-trusted-root"}\n',
        }
        repositories = {
            "https://tuf-repo.github.com": "github",
            "https://tuf-repo-cdn.sigstore.dev": "sigstore",
        }
        requests: list[str] = []
        runner_commands: list[tuple[str, ...]] = []

        def fetch(url: str, maximum: int) -> bytes:
            requests.append(url)
            for repository, role in repositories.items():
                if url.startswith(repository):
                    suffix = url.removeprefix(repository + "/")
                    if suffix == "timestamp.json":
                        return json.dumps(
                            {
                                "signed": {
                                    "version": 2,
                                    "meta": {"snapshot.json": {"version": 2}},
                                }
                            },
                            separators=(",", ":"),
                        ).encode()
                    if suffix == "2.snapshot.json":
                        return json.dumps(
                            {
                                "signed": {
                                    "version": 2,
                                    "meta": {"targets.json": {"version": 2}},
                                }
                            },
                            separators=(",", ":"),
                        ).encode()
                    if suffix == "2.targets.json":
                        return json.dumps(
                            {
                                "signed": {
                                    "version": 2,
                                    "targets": {
                                        "trusted_root.json": {
                                            "length": len(trusted[role]),
                                            "hashes": {
                                                "sha256": hashlib.sha256(
                                                    trusted[role]
                                                ).hexdigest()
                                            },
                                        }
                                    },
                                }
                            },
                            separators=(",", ":"),
                        ).encode()
                    if suffix.endswith(".trusted_root.json"):
                        return trusted[role]
                    if suffix == (
                        "4.root.json" if role == "github" else "15.root.json"
                    ):
                        return successors[role]
                    if suffix.endswith(".root.json"):
                        raise TUFMetadataNotFound(url)
            if "cli/v2.97.0" in url:
                return roots["github"]
            if "sigstore-go/v1.2.2" in url:
                return roots["sigstore"]
            raise AssertionError(url)

        def run(command, **kwargs):
            runner_commands.append(command)
            if command[1:] == ("--version",):
                return subprocess.CompletedProcess(
                    command, 0, stdout=b"2.97.0+animemo.2\n", stderr=b""
                )
            request = json.loads(
                Path(command[command.index("--request") + 1]).read_text()
            )
            def claim(role: str) -> dict[str, object]:
                return {
                    "revokedSignerKeyIds": [],
                    "snapshotVersion": 2,
                    "supersededMaterialIdentities": [],
                    "targetsVersion": 2,
                    "timestampVersion": 2,
                    "trustedRootSha256": _digest(trusted[role]),
                    "tufRootSha256": _digest(successors[role]),
                    "tufRootVersion": 4 if role == "github" else 15,
                }

            result = {
                "authorityRole": "TRUST_METADATA_ONLY",
                "fromProfileIdentity": request["fromProfileIdentity"],
                "github": claim("github"),
                "schemaVersion": 1,
                "sigstore": claim("sigstore"),
            }
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    result, sort_keys=True, separators=(",", ":")
                ).encode()
                + b"\n",
                stderr=b"",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = root / "offline-release-verifier"
            verifier.write_bytes(b"linux-amd64-verifier")
            kit = root / "pretrust-v2"

            tracks = {
                role: {
                    **dict(trust_bootstrap._TRACKS[role]),
                    "bootstrapSha256": _digest(roots[role]),
                }
                for role in ("github", "sigstore")
            }
            with mock.patch.object(trust_bootstrap, "_TRACKS", tracks):
                receipt = build_initial_trust_kit(
                    verifier=verifier,
                    output=kit,
                    fetcher=fetch,
                    runner=run,
                )
            profile = TrustProfile.from_bootstrap_record(
                json.loads((kit / "trust-profile.json").read_text())
            )

            self.assertEqual(receipt["profileIdentity"], profile.identity)
            self.assertEqual(profile.github_tuf_root_version, 4)
            self.assertEqual(profile.sigstore_tuf_root_version, 15)
            self.assertEqual(
                {path.name for path in kit.iterdir()},
                {
                    "github-trusted-root.jsonl",
                    "github-tuf-root.json",
                    "initial-trust-bootstrap.json",
                    "offline-release-verifier",
                    "sigstore-trusted-root.jsonl",
                    "sigstore-tuf-root.json",
                    "trust-profile.json",
                },
            )
            self.assertTrue(any("targets/" in url for url in requests))
            self.assertTrue(runner_commands)
            self.assertTrue(all(command[0] != str(verifier) for command in runner_commands))
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("path-based trust-kit read is forbidden"),
            ):
                trust_bootstrap.validate_initial_trust_kit(kit)

            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )
            archive = root / "installer-materials.tar"
            build_installer_materials(
                Path(__file__).resolve().parents[2],
                wheelhouse=wheelhouse,
                output=archive,
                initial_trust_kit=kit,
            )
            with tarfile.open(archive, "r:") as bundle:
                names = {item.name for item in bundle.getmembers()}
            self.assertTrue(
                {
                    "release/release_attestation_verifier/pretrust-v2/"
                    + name
                    for name in trust_bootstrap.INITIAL_TRUST_KIT_FILES
                }.issubset(names)
            )

    def test_existing_output_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pretrust-v2"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "必须不存在"):
                build_initial_trust_kit(
                    verifier=Path(directory) / "missing",
                    output=output,
                    fetcher=lambda *_: b"",
                    runner=lambda *_args, **_kwargs: None,
                )


if __name__ == "__main__":
    unittest.main()
