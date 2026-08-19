from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer.bootstrap import (
    _REQUIRED_RUNTIME_MODULES,
    BootstrapAuthorityError,
    BootstrapPrivilegeGate,
    ProductionBootstrapPrivilegeGate,
    _validate_protected_runtime_sources,
    authorize_online_stage0,
    close_bootstrap_authorization,
    commit_bootstrap_authorization,
)
from updater.trust_lifecycle import TrustCommitReceipt


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _payload(archive: Path) -> dict[str, object]:
    data = archive.read_bytes()
    return {
        "schemaVersion": 1,
        "state": "PRIVILEGE_ALLOWED",
        "repository": "yanyuhanyue/AniMemo",
        "tag": "v1.1.0-rc.1",
        "releaseCommit": "1" * 40,
        "releaseAttestationIdentity": "sha256:" + "2" * 64,
        "installerMaterials": {
            "path": str(archive),
            "sha256": _digest(data),
            "size": len(data),
        },
        "stage0": {
            "model": "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY",
            "carrier": "GH_2_97_0_EXACT_FROM_OFFICIAL_SIGNED_APT",
            "verifierIdentity": "gh:2.97.0",
        },
        "verifiedAt": "2026-08-19T00:00:00Z",
    }


class BootstrapPrivilegeGateTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == "posix" and os.geteuid() != 0,
        "受保护运行时身份测试需要真实 root 所有权语义",
    )
    def test_protected_runtime_sources_must_match_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materials = root / "materials"
            materials.mkdir(mode=0o700)
            archive = root / "installer-materials.tar"
            module_files: dict[str, Path] = {}
            with tarfile.open(archive, "w:", format=tarfile.USTAR_FORMAT) as bundle:
                for name in sorted(_REQUIRED_RUNTIME_MODULES):
                    relative = name.replace(".", "/") + ".py"
                    data = f"# qualified {name}\n".encode()
                    target = materials.joinpath(*relative.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o644)
                    module_files[name] = target
                    member = tarfile.TarInfo(relative)
                    member.size = len(data)
                    member.mode = 0o644
                    member.mtime = 0
                    bundle.addfile(member, io.BytesIO(data))

            with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root):
                commit_bootstrap_authorization(_payload(archive))
                capability = BootstrapPrivilegeGate().consume(
                    version="v1.1.0-rc.1",
                    release_commit="1" * 40,
                )
                _validate_protected_runtime_sources(
                    capability,
                    module_files=module_files,
                )
                next(iter(module_files.values())).write_text("# tampered\n")
                with self.assertRaisesRegex(
                    BootstrapAuthorityError,
                    "BOOTSTRAP_RUNTIME_SOURCE_IDENTITY_MISMATCH",
                ):
                    _validate_protected_runtime_sources(
                        capability,
                        module_files=module_files,
                    )

    def test_production_gate_provisions_trust_before_returning_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            lifecycle = mock.Mock()

            with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root):
                authorization = commit_bootstrap_authorization(_payload(protected))
                lifecycle.provision_initial.return_value = TrustCommitReceipt(
                    commit_identity="sha256:" + "3" * 64,
                    profile_identity="sha256:" + "4" * 64,
                    generation=1,
                    authorization_identity=authorization.identity,
                )
                with mock.patch(
                    "updater.trust_lifecycle.ProductionTrustLifecycle.production",
                    return_value=lifecycle,
                ), mock.patch(
                    "installer.bootstrap._validate_protected_runtime_sources"
                ) as runtime_binding:
                    capability = ProductionBootstrapPrivilegeGate().consume(
                        version="v1.1.0-rc.1",
                        release_commit="1" * 40,
                    )

            self.assertEqual(
                capability.authorization_identity,
                authorization.identity,
            )
            lifecycle.provision_initial.assert_called_once_with(capability)
            runtime_binding.assert_called_once_with(capability)

    def test_online_stage0_uses_fixed_gh_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            completed = (
                subprocess.CompletedProcess([], 0, "gh version 2.97.0\n", ""),
                subprocess.CompletedProcess([], 0, '{"release":"verified"}\n', ""),
                subprocess.CompletedProcess([], 0, '{"asset":"verified"}\n', ""),
            )
            with (
                mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root),
                mock.patch("installer.bootstrap.subprocess.run", side_effect=completed) as run,
            ):
                receipt = authorize_online_stage0(
                    tag="v1.1.0-rc.1",
                    release_commit="1" * 40,
                    verified_at="2026-08-19T00:00:00Z",
                )

            self.assertTrue(receipt.identity.startswith("sha256:"))
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertEqual(call.args[0][0], "/usr/bin/gh")
                self.assertEqual(
                    set(call.kwargs["env"]),
                    {"GH_PROMPT_DISABLED", "HOME", "LANG", "LC_ALL", "PATH"},
                )
                self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertIn("verify-asset", run.call_args_list[2].args[0])

    def test_old_or_malformed_gh_fails_before_release_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "installer-materials.tar").write_bytes(b"verified materials")
            old = subprocess.CompletedProcess([], 0, "gh version 2.96.0\n", "")
            with (
                mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root),
                mock.patch("installer.bootstrap.subprocess.run", return_value=old) as run,
                self.assertRaisesRegex(
                    BootstrapAuthorityError,
                    "BOOTSTRAP_STAGE0_GH_VERSION_INVALID",
                ),
            ):
                authorize_online_stage0(
                    tag="v1.1.0-rc.1",
                    release_commit="1" * 40,
                    verified_at="2026-08-19T00:00:00Z",
                )
            self.assertEqual(run.call_count, 1)

    def test_closed_authorization_binds_protected_bytes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            payload = _payload(protected)

            with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root):
                receipt = commit_bootstrap_authorization(payload)
                again = commit_bootstrap_authorization(payload)
                gate = BootstrapPrivilegeGate()
                authorized = gate.consume(
                    version="v1.1.0-rc.1",
                    release_commit="1" * 40,
                )

            self.assertEqual(receipt.identity, again.identity)
            self.assertEqual(authorized.authorization_identity, receipt.identity)
            self.assertEqual(authorized.materials_sha256, _digest(b"verified materials"))

    def test_different_second_authorization_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            first = _payload(protected)
            second = dict(first)
            second["tag"] = "v1.1.0-rc.2"

            with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root):
                commit_bootstrap_authorization(first)
                with self.assertRaisesRegex(
                    BootstrapAuthorityError,
                    "BOOTSTRAP_AUTHORIZATION_CONFLICT",
                ):
                    commit_bootstrap_authorization(second)

    def test_tamper_symlink_and_cross_release_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            payload = _payload(protected)

            with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root):
                commit_bootstrap_authorization(payload)
                protected.write_bytes(b"tampered materials")
                with self.assertRaisesRegex(
                    BootstrapAuthorityError,
                    "BOOTSTRAP_PROTECTED_MATERIAL_MISMATCH",
                ):
                    BootstrapPrivilegeGate().consume(
                        version="v1.1.0-rc.1",
                        release_commit="1" * 40,
                    )

                protected.write_bytes(b"verified materials")
                with self.assertRaisesRegex(
                    BootstrapAuthorityError,
                    "BOOTSTRAP_RELEASE_BINDING_MISMATCH",
                ):
                    BootstrapPrivilegeGate().consume(
                        version="v1.1.0-rc.2",
                        release_commit="1" * 40,
                    )

    def test_unknown_fields_and_non_production_stage0_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            protected = Path(directory) / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            payload = _payload(protected)
            payload["debug"] = True
            with self.assertRaisesRegex(
                BootstrapAuthorityError,
                "BOOTSTRAP_AUTHORIZATION_INVALID",
            ):
                close_bootstrap_authorization(payload)

            payload.pop("debug")
            payload["stage0"] = dict(payload["stage0"])
            payload["stage0"]["carrier"] = "TEST_FIXTURE"
            with (
                mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", protected.parent),
                self.assertRaisesRegex(
                    BootstrapAuthorityError,
                    "BOOTSTRAP_STAGE0_INVALID",
                ),
            ):
                close_bootstrap_authorization(payload)

    def test_receipt_is_canonical_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root):
                receipt = commit_bootstrap_authorization(_payload(protected))
            data = json.loads((root / "bootstrap-authorization.json").read_text())
            self.assertEqual(data["authorizationIdentity"], receipt.identity)
            self.assertNotIn("token", json.dumps(data).lower())
            self.assertNotIn("secret", json.dumps(data).lower())


if __name__ == "__main__":
    unittest.main()
