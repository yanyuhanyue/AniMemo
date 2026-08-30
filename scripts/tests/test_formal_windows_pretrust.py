from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError

from release.formal_windows_pretrust import (
    FORMAL_WINDOWS_PRETRUST_FILES,
    FormalWindowsPretrustedTrustMaterial,
    FormalWindowsPretrustError,
    _assert_no_reparse_components,
    _validate_windows_acl_observation,
    _WindowsAceObservation,
    _WindowsAclAuthority,
    assert_windows_private_acl,
    build_formal_windows_pretrust_kit,
    commit_windows_private_directory_snapshot,
    create_windows_private_directory,
    hold_windows_audited_system_tool_source,
    hold_windows_audited_tool,
    hold_windows_fixed_source_snapshot,
    hold_windows_private_descendant_path,
    hold_windows_private_path_authority,
    hold_windows_private_path_chain,
    hold_windows_private_snapshot,
    hold_windows_private_source_snapshot,
    hold_windows_private_tool_bundle_snapshot,
    hold_windows_private_tree_snapshot,
    hold_windows_system_tool_private_snapshot,
)
from release.trust_bootstrap import validate_initial_trust_kit
from scripts.tests.formal_windows_pretrust_fixture import (
    minimal_pe32_plus_amd64,
)
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit


class FormalWindowsPretrustTests(unittest.TestCase):
    def build(self, root: Path) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        source = create_test_initial_trust_kit(root)
        verifier = root / "formal-release-verifier.exe"
        verifier.write_bytes(minimal_pe32_plus_amd64())
        output = root / "formal-windows-amd64-pretrust-v1"
        with mock.patch(
            "release.formal_windows_pretrust.assert_windows_private_acl"
        ):
            build_formal_windows_pretrust_kit(
                verifier=verifier,
                source_initial_trust_kit=source,
                output=output,
            )
        return source, output

    def test_builder_closes_dual_platform_pretrust_without_self_authorizing_roots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output = self.build(Path(directory))

            with mock.patch(
                "release.formal_windows_pretrust.assert_windows_private_acl"
            ):
                material = FormalWindowsPretrustedTrustMaterial.load(output)

            self.assertEqual(
                {item.name for item in output.iterdir()},
                FORMAL_WINDOWS_PRETRUST_FILES,
            )
            self.assertEqual(material.profile.platform, "windows/amd64")
            self.assertEqual(
                material.profile.source_profile_identity,
                validate_initial_trust_kit(source).identity,
            )
            self.assertEqual(
                material.profile.github_trusted_root_sha256,
                "sha256:"
                + __import__("hashlib").sha256(
                    (source / "github-trusted-root.jsonl").read_bytes()
                ).hexdigest(),
            )
            self.assertNotEqual(
                material.profile.verifier_identity,
                material.profile.linux_guest_verifier_identity,
            )
            self.assertEqual(
                material.verifier_path.name, "formal-release-verifier.exe"
            )
            self.assertEqual(
                material.linux_guest_verifier_path.name,
                "offline-release-verifier",
            )

    def test_generated_profile_satisfies_closed_schema(self) -> None:
        schema_path = (
            Path(__file__).parents[2]
            / "release"
            / "formal-windows-trust-profile.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        with tempfile.TemporaryDirectory() as directory:
            _, output = self.build(Path(directory))
            profile = json.loads(
                (output / "formal-windows-trust-profile.json").read_text(
                    encoding="utf-8"
                )
            )

        Draft202012Validator(schema).validate(profile)
        profile["github"]["trustedRoot"]["binaryFormat"] = "PE32+"
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(profile)

    def test_builder_rejects_elf_or_non_amd64_windows_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_test_initial_trust_kit(root)
            verifier = root / "formal-release-verifier.exe"
            for value in (
                b"\x7fELF" + bytes(508),
                minimal_pe32_plus_amd64().replace(b"\x64\x86", b"\x4c\x01", 1),
            ):
                with self.subTest(prefix=value[:4]):
                    verifier.write_bytes(value)
                    with self.assertRaisesRegex(
                        FormalWindowsPretrustError, r"PE32\+ AMD64"
                    ):
                        with mock.patch(
                            "release.formal_windows_pretrust.assert_windows_private_acl"
                        ):
                            build_formal_windows_pretrust_kit(
                                verifier=verifier,
                                source_initial_trust_kit=source,
                                output=root / ("rejected-" + value[:2].hex()),
                            )

    def test_private_tool_bundle_copies_exact_mixed_pe_closure_and_rejects_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            source = create_windows_private_directory(
                parent, prefix="formal-tool-source"
            )
            private = create_windows_private_directory(
                parent, prefix="formal-tool-private"
            )
            executable = minimal_pe32_plus_amd64().replace(
                b"\x64\x86", b"\x4c\x01", 1
            ).replace(b"\x0b\x02", b"\x0b\x01", 1)
            dependency = minimal_pe32_plus_amd64()
            (source / "tool.exe").write_bytes(executable)
            (source / "dependency.dll").write_bytes(dependency)
            expected = {
                "dependency.dll": "sha256:"
                + hashlib.sha256(dependency).hexdigest(),
                "tool.exe": "sha256:" + hashlib.sha256(executable).hexdigest(),
            }

            with hold_windows_private_tool_bundle_snapshot(
                source,
                expected_file_identities=expected,
                expected_pe_machines={
                    "dependency.dll": 0x8664,
                    "tool.exe": 0x014C,
                },
                executable_name="tool.exe",
                private_root=private,
            ) as held:
                self.assertEqual(held.executable, private / "tool.exe")
                self.assertEqual(set(held.file_identities), set(expected))
                self.assertRegex(held.aggregate_identity, r"^sha256:[0-9a-f]{64}$")
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        (private / "tool.exe").write_bytes(b"tamper")

            rejected = create_windows_private_directory(
                parent, prefix="formal-tool-rejected"
            )
            with self.assertRaisesRegex(
                FormalWindowsPretrustError, "identity"
            ):
                with hold_windows_private_tool_bundle_snapshot(
                    source,
                    expected_file_identities={**expected, "tool.exe": "sha256:" + "0" * 64},
                    expected_pe_machines={
                        "dependency.dll": 0x8664,
                        "tool.exe": 0x014C,
                    },
                    executable_name="tool.exe",
                    private_root=rejected,
                ):
                    self.fail("tampered tool bundle was accepted")

    def test_fixed_source_snapshot_holds_exact_unicode_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            source = create_windows_private_directory(
                parent, prefix="formal-source"
            )
            first = source / "Ubuntu 64 位.vmx"
            second = source / "Ubuntu 64 位-000001.vmdk"
            first.write_bytes(b"vmx")
            second.write_bytes(b"disk")
            names = tuple(sorted((first.name, second.name)))

            with hold_windows_fixed_source_snapshot(
                source, relative_files=names
            ) as held:
                self.assertEqual(held.relative_files, names)
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        first.write_bytes(b"rebound")

            (source / "unexpected.log").write_bytes(b"new")
            with self.assertRaisesRegex(
                FormalWindowsPretrustError, "inventory"
            ):
                with hold_windows_fixed_source_snapshot(
                    source, relative_files=names
                ):
                    self.fail("changed source inventory was accepted")

    def test_private_source_snapshot_excludes_transient_public_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            source = create_windows_private_directory(
                parent, prefix="formal-public-source"
            )
            private = create_windows_private_directory(
                parent, prefix="formal-private-source"
            )
            vmx = source / "Ubuntu 64 位.vmx"
            disk = source / "Ubuntu 64 位-s001.vmdk"
            ignored_log = source / "vmware.log"
            vmx.write_bytes(b"vmx-authority")
            disk.write_bytes(b"disk-authority")
            ignored_log.write_bytes(b"ignored-log")
            inventory = tuple(sorted(path.name for path in source.iterdir()))
            expected = {
                path.name: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (vmx, disk)
            }

            with hold_windows_private_source_snapshot(
                source,
                source_inventory=inventory,
                expected_file_identities=expected,
                private_root=private,
            ) as held:
                transient = source / "injected.vmdk"
                # Simulate add -> would-be wildcard copy -> remove.  The exact
                # copy set has already closed, so the child can never enter the
                # private source even when the broad source DACL permits it.
                transient.write_bytes(b"attacker")
                self.assertFalse((held.root / transient.name).exists())
                transient.unlink()
                self.assertEqual(
                    tuple(sorted(path.name for path in held.root.iterdir())),
                    tuple(sorted(expected)),
                )

            self.assertEqual(
                (private / vmx.name).read_bytes(), b"vmx-authority"
            )
            self.assertFalse((private / ignored_log.name).exists())

    def test_loader_rejects_hash_tamper_root_swap_and_identity_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, output = self.build(root)
            cases = ("hash-tamper", "root-swap", "identity-reuse")
            for index, case in enumerate(cases):
                with self.subTest(case=case):
                    _, kit = self.build(root / str(index))
                    if case == "hash-tamper":
                        with (kit / "formal-release-verifier.exe").open("ab") as stream:
                            stream.write(b"tamper")
                    elif case == "root-swap":
                        github = kit / "github-trusted-root.jsonl"
                        sigstore = kit / "sigstore-trusted-root.jsonl"
                        github_value = github.read_bytes()
                        github.write_bytes(sigstore.read_bytes())
                        sigstore.write_bytes(github_value)
                    else:
                        profile_path = kit / "formal-windows-trust-profile.json"
                        profile = json.loads(profile_path.read_text())
                        profile["windowsHostVerifier"]["sha256"] = profile[
                            "linuxGuestVerifier"
                        ]["sha256"]
                        profile_path.write_text(
                            json.dumps(
                                profile,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            encoding="utf-8",
                        )
                    with (
                        mock.patch(
                            "release.formal_windows_pretrust.assert_windows_private_acl"
                        ),
                        self.assertRaises(FormalWindowsPretrustError),
                    ):
                        FormalWindowsPretrustedTrustMaterial.load(kit)

            self.assertTrue(output.is_dir())

    def test_acl_contract_rejects_owner_other_writer_and_unknown_ace(self) -> None:
        unsafe = (
            _WindowsAceObservation(0, 0, 0x40000000, False),
            _WindowsAceObservation(5, 0, 0x10000000, False),
            _WindowsAceObservation(9, 0, 0, False),
        )
        with self.assertRaisesRegex(FormalWindowsPretrustError, "owner"):
            _validate_windows_acl_observation(owner_trusted=False, aces=())
        for ace in unsafe:
            with self.subTest(ace=ace), self.assertRaises(
                FormalWindowsPretrustError
            ):
                _validate_windows_acl_observation(
                    owner_trusted=True, aces=(ace,)
                )
        _validate_windows_acl_observation(
            owner_trusted=True,
            aces=(
                _WindowsAceObservation(0, 0, 0x40000000, True),
                _WindowsAceObservation(0, 0x08, 0x10000000, False),
            ),
        )

    def test_token_access_check_detects_enabled_group_mutation(self) -> None:
        authority = object.__new__(_WindowsAclAuthority)
        calls: list[int] = []
        closed: list[int] = []

        def duplicate(_token, _level, destination):
            ctypes.cast(
                destination, ctypes.POINTER(ctypes.c_void_p)
            ).contents.value = 0x1234
            return True

        def access_check(
            _descriptor,
            _token,
            desired,
            _mapping,
            privileges,
            privilege_size,
            _granted,
            access_status,
        ):
            calls.append(desired)
            ctypes.cast(
                access_status, ctypes.POINTER(ctypes.c_long)
            ).contents.value = desired == 0x40
            return True

        authority.advapi32 = SimpleNamespace(
            DuplicateToken=duplicate,
            AccessCheck=access_check,
        )
        authority.kernel32 = SimpleNamespace(
            CloseHandle=lambda handle: closed.append(handle.value) or True
        )
        with mock.patch.object(
            ctypes, "set_last_error", create=True
        ), mock.patch.object(
            ctypes, "get_last_error", return_value=122, create=True
        ):
            allowed = authority._current_token_has_mutation_access(
                descriptor=ctypes.c_void_p(0x5678),
                token=ctypes.c_void_p(0x9012),
                mutation_mask=0x40 | 0x10000 | 0x40000 | 0x80000,
            )

        self.assertTrue(allowed)
        self.assertEqual(calls, [0x40])
        self.assertEqual(closed, [0x1234])

    def test_token_access_check_honors_deny_only_or_ordered_group_denial(self) -> None:
        authority = object.__new__(_WindowsAclAuthority)
        calls: list[int] = []
        closed: list[int] = []

        def duplicate(_token, _level, destination):
            ctypes.cast(
                destination, ctypes.POINTER(ctypes.c_void_p)
            ).contents.value = 0x1234
            return True

        def access_check(
            _descriptor,
            _token,
            desired,
            _mapping,
            privileges,
            privilege_size,
            _granted,
            access_status,
        ):
            calls.append(desired)
            ctypes.cast(
                access_status, ctypes.POINTER(ctypes.c_long)
            ).contents.value = 0
            return True

        authority.advapi32 = SimpleNamespace(
            DuplicateToken=duplicate,
            AccessCheck=access_check,
        )
        authority.kernel32 = SimpleNamespace(
            CloseHandle=lambda handle: closed.append(handle.value) or True
        )
        with mock.patch.object(
            ctypes, "set_last_error", create=True
        ), mock.patch.object(
            ctypes, "get_last_error", return_value=122, create=True
        ):
            allowed = authority._current_token_has_mutation_access(
                descriptor=ctypes.c_void_p(0x5678),
                token=ctypes.c_void_p(0x9012),
                mutation_mask=0x40 | 0x10000 | 0x40000 | 0x80000,
            )

        self.assertFalse(allowed)
        self.assertEqual(
            calls,
            [
                0x40,
                0x10000,
                0x40000,
                0x80000,
            ],
        )
        self.assertEqual(closed, [0x1234])

    def test_parent_reparse_observation_is_rejected(self) -> None:
        clean = mock.Mock()
        clean.lstat.return_value = SimpleNamespace(st_file_attributes=0)
        reparse = mock.Mock()
        reparse.lstat.return_value = SimpleNamespace(st_file_attributes=0x400)

        with self.assertRaisesRegex(FormalWindowsPretrustError, "reparse"):
            _assert_no_reparse_components((clean, reparse))

    @unittest.skipUnless(os.name == "nt", "Windows DACL contract")
    def test_private_directory_is_created_with_protected_trusted_dacl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = create_windows_private_directory(
                Path(directory), prefix="formal-contract"
            )

            assert_windows_private_acl(private)

    @unittest.skipUnless(os.name == "nt", "Windows handle sharing contract")
    def test_boundary_and_snapshot_handles_block_parent_rebind_and_file_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = create_windows_private_directory(
                Path(directory), prefix="formal-handle"
            )
            with hold_windows_private_path_chain(private):
                material = private / "input.bin"
                material.write_bytes(b"held authority")
                executable = private / "held-python.exe"
                executable.write_bytes(Path(sys.executable).read_bytes())
                with self.assertRaises(OSError):
                    private.rename(private.with_name(private.name + "-rebound"))
                with hold_windows_private_snapshot(
                    private,
                    relative_files=("held-python.exe", "input.bin"),
                    root_already_held=True,
                ):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; "
                            f"assert Path({str(material)!r}).read_bytes() == b'held authority'",
                        ],
                        check=False,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    held_process = subprocess.run(
                        [str(executable), "-c", "raise SystemExit(0)"],
                        check=False,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(
                        held_process.returncode, 0, held_process.stderr
                    )
                    self.assertEqual(material.read_bytes(), b"held authority")
                    with self.assertRaises(OSError):
                        material.write_bytes(b"rebound")
                    with self.assertRaises(OSError):
                        private.rename(private.with_name(private.name + "-rebound"))

    @unittest.skipUnless(os.name == "nt", "Windows held commit contract")
    def test_private_snapshot_commit_renames_then_reopens_held_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = create_windows_private_directory(
                Path(directory), prefix="formal-commit-parent"
            )
            staging = create_windows_private_directory(
                parent, prefix="formal-commit-staging"
            )
            destination = parent / "formal-committed"
            first = staging / "receipt.json"
            second = staging / "verifier.exe"
            first.write_bytes(b'{"result":"PASS"}')
            second.write_bytes(minimal_pe32_plus_amd64())
            digests = {
                item.name: "sha256:" + hashlib.sha256(item.read_bytes()).hexdigest()
                for item in (first, second)
            }
            identities = {
                item.name: (item.stat().st_dev, item.stat().st_ino)
                for item in (first, second)
            }

            with commit_windows_private_directory_snapshot(
                staging,
                destination,
                relative_files=("receipt.json", "verifier.exe"),
                expected_file_identities=digests,
            ) as committed:
                self.assertEqual(committed, destination)
                self.assertFalse(staging.exists())
                self.assertEqual(
                    {
                        item.name: (item.stat().st_dev, item.stat().st_ino)
                        for item in committed.iterdir()
                    },
                    identities,
                )
                with self.assertRaises(OSError):
                    (committed / "receipt.json").write_bytes(b"tamper")
                with self.assertRaises(OSError):
                    committed.rename(parent / "rebound")

            self.assertEqual(
                (destination / "receipt.json").read_bytes(),
                b'{"result":"PASS"}',
            )

    @unittest.skipUnless(os.name == "nt", "Windows private tree contract")
    def test_private_tree_copies_only_pinned_files_and_holds_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "public-candidate"
            (source / "installer-root" / "scripts").mkdir(parents=True)
            candidate = source / "candidate-input.json"
            runner = source / "installer-root" / "scripts" / "runner.py"
            unexpected = source / "injected.txt"
            candidate.write_bytes(b'{"candidate":"fixed"}')
            runner.write_bytes(b"print('fixed')\n")
            unexpected.write_bytes(b"never-authoritative")
            expected = {
                path.relative_to(source).as_posix(): (
                    "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                )
                for path in (candidate, runner)
            }
            private_parent = create_windows_private_directory(
                temporary, prefix="formal-tree-parent"
            )
            private = create_windows_private_directory(
                private_parent, prefix="formal-tree"
            )

            with hold_windows_private_tree_snapshot(
                source,
                expected_file_identities=expected,
                private_root=private,
            ) as snapshot:
                self.assertEqual(set(snapshot.file_identities), set(expected))
                self.assertFalse((private / "injected.txt").exists())
                self.assertEqual(
                    (private / "installer-root" / "scripts" / "runner.py").read_bytes(),
                    runner.read_bytes(),
                )
                with self.assertRaises(OSError):
                    candidate.write_bytes(b"source-rebound")
                with self.assertRaises(OSError):
                    (private / "candidate-input.json").write_bytes(
                        b"private-rebound"
                    )

    @unittest.skipUnless(os.name == "nt", "Windows audited tool contract")
    def test_audited_tool_holder_allows_create_process_and_blocks_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = create_windows_private_directory(
                Path(directory), prefix="formal-audited-tool"
            )
            tool = private / "audited-python.exe"
            tool.write_bytes(Path(sys.executable).read_bytes())

            with hold_windows_audited_tool(tool):
                completed = subprocess.run(
                    [str(tool), "-c", "raise SystemExit(0)"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                with self.assertRaises(OSError):
                    tool.rename(tool.with_name(tool.name + ".rebound"))

    @unittest.skipUnless(os.name == "nt", "Windows System32 source contract")
    def test_system_tool_source_is_pinned_then_executed_only_from_private_copy(
        self,
    ) -> None:
        import hashlib

        system32 = Path(os.environ["SystemRoot"]) / "System32"
        sources = (
            system32 / "robocopy.exe",
            system32 / "OpenSSH" / "ssh.exe",
            system32 / "OpenSSH" / "scp.exe",
            system32 / "OpenSSH" / "ssh-keygen.exe",
            system32 / "libcrypto.dll",
        )
        for audited in sources:
            with self.subTest(audited=audited):
                identity = (
                    "sha256:"
                    + hashlib.sha256(audited.read_bytes()).hexdigest()
                )
                with hold_windows_audited_system_tool_source(
                    audited, expected_sha256=identity
                ) as held_source:
                    self.assertEqual(
                        "sha256:"
                        + hashlib.sha256(held_source.read_bytes()).hexdigest(),
                        identity,
                    )

        source = sources[0]
        identity = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            private = create_windows_private_directory(
                Path(directory), prefix="formal-system-tool"
            )
            with hold_windows_system_tool_private_snapshot(
                source,
                expected_sha256=identity,
                private_root=private,
                destination_name="robocopy.exe",
            ) as staged:
                completed = subprocess.run(
                    [str(staged), "/?"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                self.assertIn(completed.returncode, {0, 1, 16})
                self.assertEqual(
                    "sha256:" + hashlib.sha256(staged.read_bytes()).hexdigest(),
                    identity,
                )
            with self.assertRaisesRegex(
                FormalWindowsPretrustError, "identity"
            ):
                with hold_windows_audited_system_tool_source(
                    source, expected_sha256="sha256:" + "0" * 64
                ):
                    self.fail("wrong identity was accepted")
            private_tool = private / "unexpected.exe"
            private_tool.write_bytes(source.read_bytes())
            with self.assertRaisesRegex(
                FormalWindowsPretrustError, "allowlist"
            ):
                with hold_windows_audited_system_tool_source(
                    private_tool, expected_sha256=identity
                ):
                    self.fail("non-System32 source was accepted")

    @unittest.skipUnless(os.name == "nt", "Windows path-chain handle contract")
    def test_path_chain_blocks_ancestor_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = create_windows_private_directory(
                Path(directory), prefix="formal-chain"
            )
            ancestor = private / "ancestor"
            work = ancestor / "work"
            ancestor.mkdir()
            work.mkdir()

            with hold_windows_private_path_chain(work):
                with self.assertRaises(OSError):
                    ancestor.rename(ancestor.with_name("rebound"))

    @unittest.skipUnless(os.name == "nt", "Windows shared path authority")
    def test_path_authority_extends_child_without_reopening_ancestors(self) -> None:
        outer = create_windows_private_directory(
            Path("E:/"), prefix="formal-shared-chain"
        )
        material_parent = create_windows_private_directory(
            outer, prefix="candidate-material-parent"
        )
        child = create_windows_private_directory(
            material_parent, prefix="candidate-material"
        )
        try:
            with hold_windows_private_path_authority(outer) as parent_authority:
                with hold_windows_private_descendant_path(
                    parent_authority, child
                ):
                    with self.assertRaises(OSError):
                        outer.rename(outer.with_name(outer.name + "-rebound"))
                    with self.assertRaises(OSError):
                        material_parent.rename(
                            material_parent.with_name(material_parent.name + "-rebound")
                        )
                    with self.assertRaises(OSError):
                        child.rename(child.with_name(child.name + "-rebound"))
            with self.assertRaises(FormalWindowsPretrustError):
                _ = parent_authority.root
        finally:
            if outer.exists():
                shutil.rmtree(outer)

    def test_acl_failure_with_dirty_descriptor_releases_exactly_once(self) -> None:
        class Function:
            def __init__(self, implementation=None):
                self.implementation = implementation or (lambda *_args: 0)
                self.calls = []
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                self.calls.append(args)
                return self.implementation(*args)

        class Library:
            def __init__(self):
                self.functions = {}

            def __getattr__(self, name):
                return self.functions.setdefault(name, Function())

        libraries = {"advapi32": Library(), "kernel32": Library()}

        def get_named(_path, _kind, _flags, _owner, _group, _dacl, _sacl, sd):
            ctypes.cast(sd, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x1234
            return 0

        libraries["advapi32"].functions["GetNamedSecurityInfoW"] = Function(
            get_named
        )
        libraries["kernel32"].functions["LocalFree"] = Function(
            lambda _pointer: 0
        )

        def loader(name, *, use_last_error):
            self.assertTrue(use_last_error)
            return libraries[name]

        authority = _WindowsAclAuthority(dll_loader=loader)
        with self.assertRaisesRegex(FormalWindowsPretrustError, "owner/DACL"):
            authority.inspect_acl(Path("dirty-descriptor"))

        local_free = libraries["kernel32"].functions["LocalFree"]
        self.assertEqual(len(local_free.calls), 1)
        self.assertEqual(local_free.argtypes, (ctypes.c_void_p,))
        self.assertIs(local_free.restype, ctypes.c_void_p)


if __name__ == "__main__":
    unittest.main()
