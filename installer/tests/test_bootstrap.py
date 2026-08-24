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
    _GH_VERSION,
    _REQUIRED_RUNTIME_MODULES,
    BootstrapAuthorityError,
    BootstrapPrivilegeGate,
    GhVersionOutputError,
    ProductionBootstrapPrivilegeGate,
    _validate_protected_runtime_sources,
    authorize_online_stage0,
    close_bootstrap_authorization,
    commit_bootstrap_authorization,
    parse_gh_cli_version_output,
)
from scripts.tests.trust_kit_fixture import authority_test_namespace
from updater.trust_lifecycle import TrustCommitReceipt

GH_VERSION_FIXTURE = (
    Path(__file__).with_name("fixtures") / "gh-version-2.97.0.txt"
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _official_mirror_stage0_contract() -> str:
    source = (PROJECT_ROOT / "docs" / "distribution-transports-v1.1.md").read_text(
        encoding="utf-8"
    )
    return source.split("# OFFICIAL_MIRROR_STAGE0_BEGIN", 1)[1].split(
        "# OFFICIAL_MIRROR_STAGE0_END", 1
    )[0]


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


class GitHubCliVersionOutputContractTests(unittest.TestCase):
    def test_pinned_official_output_contract(self) -> None:
        parsed = parse_gh_cli_version_output(GH_VERSION_FIXTURE.read_bytes())

        self.assertEqual(parsed.executable_name, "gh")
        self.assertEqual(parsed.semantic_version, _GH_VERSION)
        self.assertEqual(
            parsed.first_line,
            "gh version 2.97.0 (2026-07-31)",
        )
        self.assertTrue(parsed.metadata_present)

    def test_parser_accepts_supported_line_endings_and_display_metadata(self) -> None:
        supported = (
            b"gh version 2.97.0\n",
            b"gh version 2.97.0\r\n",
            b"gh version 2.97.0 (2026-07-31)\n",
            b"gh version 2.97.0 (2026-07-31)\r\n",
            b"gh version 2.97.0\nhttps://github.com/cli/cli/releases/tag/v2.97.0\n",
            b"gh version 2.97.0 future display metadata\n",
            GH_VERSION_FIXTURE.read_bytes(),
        )
        for output in supported:
            with self.subTest(output=output):
                self.assertEqual(
                    parse_gh_cli_version_output(output).semantic_version,
                    "2.97.0",
                )

    def test_parser_rejects_malformed_deceptive_and_wrong_versions(self) -> None:
        rejected = (
            (b"", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"github version 2.97.0\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"gh version v2.97.0\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"gh version 2.97\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"gh version 2.97.0.1\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"gh version 2.97.00\n", "GH_VERSION_MISMATCH"),
            (b"gh version 2.97.0evil\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"gh version 2.97.0-rc.1\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"gh version 2.96.0\n", "GH_VERSION_MISMATCH"),
            (b"gh version 2.98.0\n", "GH_VERSION_MISMATCH"),
            (b"gh version 3.0.0\n", "GH_VERSION_MISMATCH"),
            (b" gh version 2.97.0\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (b"prefix gh version 2.97.0\n", "GH_VERSION_OUTPUT_MALFORMED"),
            (
                b"https://github.com/cli/cli/releases/tag/v2.97.0\ngh version 2.97.0\n",
                "GH_VERSION_OUTPUT_MALFORMED",
            ),
            (b"gh version 2.97.0\x00\n", "GH_VERSION_OUTPUT_CONTROL_CHARACTER"),
            (b"gh version 2.97.0\x1b[0m\n", "GH_VERSION_OUTPUT_CONTROL_CHARACTER"),
            (b"gh version 2.97.0\xff\n", "GH_VERSION_OUTPUT_INVALID_UTF8"),
            (b"gh version 2.97.0 " + b"x" * 4096, "GH_VERSION_OUTPUT_TOO_LARGE"),
            (
                b"https://example.test/2.97.0\n",
                "GH_VERSION_OUTPUT_MALFORMED",
            ),
            (b"gh version 2.96.0 metadata 2.97.0\n", "GH_VERSION_MISMATCH"),
        )
        for output, code in rejected:
            with self.subTest(output=output, code=code):
                with self.assertRaises(GhVersionOutputError) as raised:
                    parse_gh_cli_version_output(output)
                self.assertEqual(raised.exception.code, code)


class OfficialMirrorStage0ContractTests(unittest.TestCase):
    def test_github_authority_precedes_fixed_mirror_acquisition_and_execution(self) -> None:
        stage0 = _official_mirror_stage0_contract()
        release_verify = stage0.index('/usr/bin/gh release verify "$EXACT_TAG"')
        mirror_download = stage0.index(
            '/usr/bin/curl --proto \'=https\' --tlsv1.2 --location --max-redirs 0'
        )
        candidate_verify = stage0.index(
            '/usr/bin/gh release verify-asset "$EXACT_TAG" "$MIRROR_CANDIDATE"'
        )
        protected_copy = stage0.index(
            '/usr/bin/install -o root -g root -m 0600 "$MIRROR_CANDIDATE" "$HANDOFF"'
        )
        protected_verify = stage0.index(
            '/usr/bin/gh release verify-asset "$EXACT_TAG" "$PROTECTED"'
        )
        extraction = stage0.index('/usr/bin/tar -xf "$PROTECTED"')
        execution = stage0.index("-m installer \\")

        self.assertLess(release_verify, mirror_download)
        self.assertLess(mirror_download, candidate_verify)
        self.assertLess(candidate_verify, protected_copy)
        self.assertLess(protected_copy, protected_verify)
        self.assertLess(protected_verify, extraction)
        self.assertLess(extraction, execution)
        self.assertIn(
            'MIRROR_URL="https://download.animemo.cc/yanyuhanyue/AniMemo/releases/download/$EXACT_TAG/installer-materials.tar"',
            stage0,
        )
        self.assertIn("--source official-mirror", stage0)
        self.assertNotIn("gh release download", stage0)
        self.assertNotIn("download.animemo.app", stage0)
        self.assertNotIn("curl |", stage0)
        self.assertNotIn("rm -rf", stage0)

    def test_failed_protected_reverification_rolls_back_only_this_invocation(self) -> None:
        stage0 = _official_mirror_stage0_contract()
        self.assertIn("created_protected=0", stage0)
        self.assertIn("created_protected_root=0", stage0)
        self.assertIn("completed=0", stage0)
        self.assertIn('test "$created_protected" = 0 ||', stage0)
        self.assertIn(
            'test "$created_protected_root" = 1 && test -e "$PROTECTED_ROOT"',
            stage0,
        )
        self.assertIn('/usr/bin/rm -f -- "$PROTECTED"', stage0)
        self.assertIn('test ! -L "$path"', stage0)
        self.assertIn("assert_safe_root_directory /var/lib ''", stage0)
        self.assertLess(
            stage0.index('test ! -e "$PROTECTED" && test ! -L "$PROTECTED"'),
            stage0.index('/usr/bin/ln "$HANDOFF" "$PROTECTED"'),
        )
        self.assertLess(
            stage0.index("created_protected=1"),
            stage0.index('/usr/bin/ln "$HANDOFF" "$PROTECTED"'),
        )
        self.assertLess(
            stage0.index(
                '/usr/bin/gh release verify-asset "$EXACT_TAG" "$PROTECTED"'
            ),
            stage0.index("completed=1"),
        )
        self.assertEqual(stage0.count("--max-redirs 0"), 1)
        self.assertIn("--connect-timeout 30 --max-time 900", stage0)
        self.assertIn("--retry 2 --retry-delay 10 --retry-max-time 600", stage0)

    @unittest.skipUnless(os.name == "posix", "Stage-0 shell transaction requires POSIX")
    def test_candidate_verification_failure_removes_every_stage0_path(self) -> None:
        stage0 = _official_mirror_stage0_contract()
        start = stage0.index('STAGE0_DIRECTORY="$(/usr/bin/mktemp -d)"')
        end = stage0.index('MIRROR_URL="https://download.animemo.cc', start)
        transaction = stage0[start:end].replace(
            'STAGE0_DIRECTORY="$(/usr/bin/mktemp -d)"',
            'STAGE0_DIRECTORY="$TEST_ROOT/candidate"\n'
            '/usr/bin/install -d -m 0700 -- "$STAGE0_DIRECTORY"',
        )
        script = (
            "set -euo pipefail\n"
            + transaction
            + '\nprintf "untrusted" > "$MIRROR_CANDIDATE"\n'
            + "/bin/false # injected first verify-asset failure\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                env={"TEST_ROOT": str(root), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "Stage-0 root transaction requires POSIX")
    def test_protected_reverification_failure_rolls_back_created_root_tree(self) -> None:
        sudo = Path("/usr/bin/sudo")
        if not sudo.exists() or subprocess.run(
            [str(sudo), "-n", "/bin/true"],
            check=False,
            capture_output=True,
        ).returncode != 0:
            self.skipTest("passwordless sudo is required for the root transaction test")

        stage0 = _official_mirror_stage0_contract()
        body = stage0.split("<<'ANIMEMO_PROTECTED_HANDOFF'\n", 1)[1].split(
            "\nANIMEMO_PROTECTED_HANDOFF", 1
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.tar"
            candidate.write_bytes(b"verified candidate fixture")
            counter = root / "gh-count"
            counter.write_text("0", encoding="ascii")
            counter.chmod(0o666)
            fake_gh = root / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                f'count="$(/usr/bin/cat {counter.as_posix()})"\n'
                'count="$((count + 1))"\n'
                f'/usr/bin/printf "%s" "$count" > {counter.as_posix()}\n'
                'test "$count" -lt 2\n',
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            body = body.replace(
                "ANIMEMO_ROOT=/var/lib/animemo",
                'ANIMEMO_ROOT="$TEST_ROOT/animemo"',
            ).replace(
                "PROTECTED_ROOT=/var/lib/animemo/bootstrap-authority/v1",
                'PROTECTED_ROOT="$AUTHORITY_PARENT/v1"',
            ).replace(
                "assert_safe_root_directory /var/lib ''",
                ": # /var/lib is outside the isolated test namespace",
            ).replace("/usr/bin/gh", fake_gh.as_posix())
            result = subprocess.run(
                [
                    str(sudo),
                    "-n",
                    "/usr/bin/env",
                    "-i",
                    f"TEST_ROOT={root}",
                    "EXACT_TAG=v1.1.0-rc.10",
                    f"MIRROR_CANDIDATE={candidate}",
                    "HOME=/root",
                    "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                ],
                input=body,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(counter.read_text(encoding="ascii"), "2")
            self.assertFalse((root / "animemo").exists())

    @unittest.skipUnless(os.name == "posix", "Stage-0 root transaction requires POSIX")
    def test_partial_directory_creation_failure_removes_earlier_parents(self) -> None:
        sudo = Path("/usr/bin/sudo")
        if not sudo.exists() or subprocess.run(
            [str(sudo), "-n", "/bin/true"],
            check=False,
            capture_output=True,
        ).returncode != 0:
            self.skipTest("passwordless sudo is required for the root transaction test")

        stage0 = _official_mirror_stage0_contract()
        body = stage0.split("<<'ANIMEMO_PROTECTED_HANDOFF'\n", 1)[1].split(
            "\nANIMEMO_PROTECTED_HANDOFF", 1
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.tar"
            candidate.write_bytes(b"candidate fixture")
            counter = root / "install-count"
            counter.write_text("0", encoding="ascii")
            counter.chmod(0o666)
            fake_install = root / "install"
            fake_install.write_text(
                "#!/bin/sh\n"
                f'count="$(/usr/bin/cat {counter.as_posix()})"\n'
                'count="$((count + 1))"\n'
                f'/usr/bin/printf "%s" "$count" > {counter.as_posix()}\n'
                'test "$count" -lt 2 || exit 1\n'
                'exec /usr/bin/install "$@"\n',
                encoding="utf-8",
            )
            fake_install.chmod(0o755)
            body = body.replace(
                "ANIMEMO_ROOT=/var/lib/animemo",
                'ANIMEMO_ROOT="$TEST_ROOT/animemo"',
            ).replace(
                "PROTECTED_ROOT=/var/lib/animemo/bootstrap-authority/v1",
                'PROTECTED_ROOT="$AUTHORITY_PARENT/v1"',
            ).replace(
                "assert_safe_root_directory /var/lib ''",
                ": # /var/lib is outside the isolated test namespace",
            ).replace("/usr/bin/install", fake_install.as_posix())
            result = subprocess.run(
                [
                    str(sudo),
                    "-n",
                    "/usr/bin/env",
                    "-i",
                    f"TEST_ROOT={root}",
                    "EXACT_TAG=v1.1.0-rc.10",
                    f"MIRROR_CANDIDATE={candidate}",
                    "HOME=/root",
                    "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                ],
                input=body,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(counter.read_text(encoding="ascii"), "2")
            self.assertFalse((root / "animemo").exists())


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

            with authority_test_namespace(root):
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

            with authority_test_namespace(root):
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
                subprocess.CompletedProcess([], 0, b"gh version 2.97.0\n", b""),
                subprocess.CompletedProcess([], 0, b'{"release":"verified"}\n', b""),
                subprocess.CompletedProcess([], 0, b'{"asset":"verified"}\n', b""),
            )
            with (
                authority_test_namespace(root),
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
                self.assertFalse(call.kwargs["shell"])
                self.assertFalse(call.kwargs["text"])
                self.assertTrue(call.kwargs["capture_output"])
                self.assertEqual(call.kwargs["timeout"], 120)
            self.assertIn("verify-asset", run.call_args_list[2].args[0])
            self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/gh", "version"])

    def test_online_stage0_accepts_pinned_official_gh_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "installer-materials.tar").write_bytes(b"verified materials")
            completed = (
                subprocess.CompletedProcess(
                    [],
                    0,
                    GH_VERSION_FIXTURE.read_bytes(),
                    b"",
                ),
                subprocess.CompletedProcess([], 0, b'{"release":"verified"}\n', b""),
                subprocess.CompletedProcess([], 0, b'{"asset":"verified"}\n', b""),
            )
            with (
                authority_test_namespace(root),
                mock.patch("installer.bootstrap.subprocess.run", side_effect=completed),
            ):
                receipt = authorize_online_stage0(
                    tag="v1.1.0-rc.1",
                    release_commit="1" * 40,
                    verified_at="2026-08-19T00:00:00Z",
                )

        self.assertTrue(receipt.identity.startswith("sha256:"))

    def test_old_or_malformed_gh_fails_before_release_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "installer-materials.tar").write_bytes(b"verified materials")
            old = subprocess.CompletedProcess([], 0, b"gh version 2.96.0\n", b"")
            with (
                authority_test_namespace(root),
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
            self.assertEqual(
                {path.name for path in root.iterdir()},
                {"installer-materials.tar"},
            )

    def test_stage0_records_process_failure_without_trusting_stderr(self) -> None:
        failed = subprocess.CompletedProcess(
            [],
            1,
            b"",
            b"gh version 2.97.0 (2026-07-31)\n",
        )
        with (
            mock.patch("installer.bootstrap.subprocess.run", return_value=failed),
            self.assertRaisesRegex(
                BootstrapAuthorityError,
                "BOOTSTRAP_STAGE0_GH_VERSION_INVALID",
            ) as raised,
        ):
            from installer.bootstrap import _run_stage0_gh

            _run_stage0_gh(("version",))

        self.assertEqual(raised.exception.reason, "GH_VERSION_PROCESS_FAILED")

    def test_stage0_records_exact_version_failure_reasons(self) -> None:
        from installer.bootstrap import _run_stage0_gh

        failures = (
            (
                subprocess.CompletedProcess([], 0, b"gh version 2.96.0\n", b""),
                "GH_VERSION_MISMATCH",
            ),
            (
                subprocess.CompletedProcess([], 0, b"gh version 2.97.0\xff\n", b""),
                "GH_VERSION_OUTPUT_INVALID_UTF8",
            ),
            (
                subprocess.CompletedProcess([], 0, b"gh version 2.97.0\x00\n", b""),
                "GH_VERSION_OUTPUT_CONTROL_CHARACTER",
            ),
            (
                subprocess.CompletedProcess(
                    [],
                    0,
                    b"gh version 2.97.0 " + b"x" * 4096,
                    b"",
                ),
                "GH_VERSION_OUTPUT_TOO_LARGE",
            ),
            (
                subprocess.CompletedProcess(
                    [],
                    0,
                    b"not a version\n",
                    b"gh version 2.97.0 (2026-07-31)\n",
                ),
                "GH_VERSION_OUTPUT_MALFORMED",
            ),
        )
        for completed, reason in failures:
            with self.subTest(reason=reason), mock.patch(
                "installer.bootstrap.subprocess.run",
                return_value=completed,
            ), self.assertRaises(BootstrapAuthorityError) as raised:
                _run_stage0_gh(("version",))
            self.assertEqual(
                raised.exception.code,
                "BOOTSTRAP_STAGE0_GH_VERSION_INVALID",
            )
            self.assertEqual(raised.exception.reason, reason)

    def test_stage0_records_version_process_timeout(self) -> None:
        from installer.bootstrap import _run_stage0_gh

        with mock.patch(
            "installer.bootstrap.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["/usr/bin/gh", "version"], 120),
        ), self.assertRaises(BootstrapAuthorityError) as raised:
            _run_stage0_gh(("version",))

        self.assertEqual(
            raised.exception.code,
            "BOOTSTRAP_STAGE0_GH_VERSION_INVALID",
        )
        self.assertEqual(raised.exception.reason, "GH_VERSION_PROCESS_TIMEOUT")

    def test_closed_authorization_binds_protected_bytes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "installer-materials.tar"
            protected.write_bytes(b"verified materials")
            payload = _payload(protected)

            with authority_test_namespace(root):
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

            with authority_test_namespace(root):
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

            with authority_test_namespace(root):
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
                authority_test_namespace(protected.parent),
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
            with authority_test_namespace(root):
                receipt = commit_bootstrap_authorization(_payload(protected))
            data = json.loads((root / "bootstrap-authorization.json").read_text())
            self.assertEqual(data["authorizationIdentity"], receipt.identity)
            self.assertNotIn("token", json.dumps(data).lower())
            self.assertNotIn("secret", json.dumps(data).lower())


if __name__ == "__main__":
    unittest.main()
