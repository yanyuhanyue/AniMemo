"""Real Windows preparation tests using disposable plan/bootstrap fixtures.

These tests run the production provider and fixed tools, stop before Clone copy,
and never read the configured bootstrap identity or request a sudo credential.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts import candidate_vm_harness as h
from scripts.tests.test_candidate_vm_harness import FakeProvider, _loaded


@unittest.skipUnless(os.name == "nt", "Real Windows session-key preparation")
class WindowsSessionKeyPreparationTests(unittest.TestCase):
    def setUp(self):
        if not Path("E:/").is_dir() or not h.SSH_KEYGEN.is_file():
            self.skipTest("Audited Windows host toolchain/volume unavailable")
        self.root = h.create_windows_private_directory(
            Path("E:/"), prefix="animemo-keygen-test"
        )
        self.source = self.root / "source-fixture"
        self.source.mkdir()
        (self.source / "authority.txt").write_bytes(b"disposable source fixture")
        self.bootstrap = self.root / "bootstrap-fixture"
        self.bootstrap.write_bytes(b"synthetic bootstrap fixture; never used for SSH")

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent, Path("E:/"))
        self.assertTrue(self.root.name.startswith("animemo-keygen-test-"))
        self.assertFalse(self.root.is_symlink())
        shutil.rmtree(self.root)

    def plan(self):
        loaded = _loaded(self.root)
        loaded.candidate_input["candidate_version"] = "v2.0.0-rc.1"
        with mock.patch.object(h, "load_verified_candidate", return_value=loaded):
            return h.build_harness_plan(
                verified_candidate_digest=loaded.verified_digest,
                expected_qualification_run_id=loaded.candidate_input["qualification_run_id"],
                expected_source_sha=loaded.candidate_input["source_sha"],
                expected_source_tree=loaded.candidate_input["source_tree"],
                provider=FakeProvider(),
            )

    @contextmanager
    def provider(self):
        # Only the source/bootstrap data are synthetic. Tool execution,
        # Windows authority, private layout, environment and holds are real.
        with mock.patch.multiple(
            h, SOURCE_VM_ROOT=self.source, OPENSSH_IDENTITY=self.bootstrap
        ):
            provider = h.ClosedVmwareProvider()
            with provider.execution_authority():
                root = provider._execution.root
                yield provider
            self.assertFalse(root.exists())

    def authority_at(self, provider, session_root=None):
        plan = self.plan()
        authority = provider._active_profile_authority(plan.profiles[0], plan)
        if session_root is None:
            return authority
        profile = session_root / "profiles" / "fresh_base"
        ssh = profile / "ssh"
        clone = profile / "clone" / authority.clone_identity.removeprefix("sha256:")
        return replace(
            authority, session_root=session_root, profile_root=profile,
            ssh_root=ssh, identity_file=ssh / "id_ed25519",
            known_hosts_file=ssh / "known_hosts", clone_root=clone,
            clone_vmx=clone / "Ubuntu 64 位.vmx",
            quarantine_root=session_root / "quarantine" / "fresh_base",
        )

    def test_production_preparation_generates_isolated_pairs_before_clone(self):
        public_keys = []
        with self.provider() as provider:
            for _ in range(2):
                plan = self.plan()
                authority = provider._active_profile_authority(plan.profiles[0], plan)
                self.assertEqual(len(str(authority.identity_file)), 253)
                work_root = provider._execution.work_root
                lease = provider._acquire_provider_lease(authority, work_root=work_root)
                try:
                    provider._prepare_profile_authority(authority)
                    self.assertTrue(authority.identity_file.is_file())
                    self.assertTrue(authority.identity_file.with_suffix(".pub").is_file())
                    self.assertFalse(authority.clone_root.exists())
                    public_keys.append(provider._session_public_key(authority).split()[:2])
                    with ExitStack() as held:
                        for path in (authority.identity_file, authority.identity_file.with_suffix(".pub")):
                            held.enter_context(h.hold_windows_private_file(path))
                            with self.assertRaises(PermissionError):
                                path.open("ab")
                            with self.assertRaises(PermissionError):
                                path.unlink()
                finally:
                    provider._destroy_session_key(authority)
                    provider._release_provider_lease(lease, work_root=work_root)
                self.assertFalse(authority.identity_file.exists())
                self.assertFalse(authority.identity_file.with_suffix(".pub").exists())
        self.assertNotEqual(public_keys[0], public_keys[1])

    def test_short_and_unicode_space_paths_use_real_empty_passphrase_arguments(self):
        with self.provider() as provider:
            for name in ("short", "中文 空格"):
                with self.subTest(path=name):
                    authority = self.authority_at(provider, provider._execution.work_root / name)
                    try:
                        provider._prepare_profile_authority(authority)
                        self.assertEqual(provider._session_public_key(authority).split()[0], "ssh-ed25519")
                        self.assertFalse(authority.clone_root.exists())
                    finally:
                        provider._destroy_session_key(authority)

    def test_nonzero_timeout_and_cancellation_reap_child_and_clean_partial_files(self):
        for failure in ("NONZERO_EXIT", "TIMEOUT", "CANCELLED"):
            with self.subTest(failure=failure), self.provider() as provider:
                authority = self.authority_at(provider)
                real_run = provider._runner.run
                real_popen = subprocess.Popen
                processes = []

                def spawn(*args, real_popen=real_popen, processes=processes, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                def fail_generation(argv, *, authority=authority, failure=failure, real_run=real_run, **kwargs):
                    # Synthetic partial files model an interrupted generator;
                    # the subsequent launch/exit/timeout is a real child.
                    authority.identity_file.write_bytes(b"partial private test fixture")
                    authority.identity_file.with_suffix(".pub").write_bytes(b"partial public test fixture")
                    if failure == "NONZERO_EXIT":
                        argv = tuple("invalid-test-algorithm" if part == "ed25519" else part for part in argv)
                    elif failure == "TIMEOUT":
                        kwargs["timeout"] = 0
                    return real_run(argv, **kwargs)

                with ExitStack() as patches:
                    patches.enter_context(mock.patch.object(provider._runner, "run", side_effect=fail_generation))
                    patches.enter_context(mock.patch.object(subprocess, "Popen", side_effect=spawn))
                    if failure == "CANCELLED":
                        patches.enter_context(mock.patch.object(real_popen, "communicate", side_effect=KeyboardInterrupt))
                    with self.assertRaises(h.SessionKeyCommandError) as caught:
                        provider._prepare_profile_authority(authority)
                self.assertEqual(caught.exception.code, "CANDIDATE_VM_SESSION_KEY_GENERATION_FAILED")
                diagnostic = caught.exception.public_diagnostic()
                self.assertEqual(diagnostic["kind"], failure)
                if failure == "NONZERO_EXIT":
                    self.assertNotEqual(diagnostic["returncode"], 0)
                else:
                    self.assertIsNone(diagnostic["returncode"])
                    self.assertIsNone(diagnostic["stdout_empty"])
                    self.assertIsNone(diagnostic["stderr_empty"])
                self.assertTrue(processes)
                self.assertTrue(all(process.poll() is not None for process in processes))
                self.assertFalse(authority.identity_file.exists())
                self.assertFalse(authority.identity_file.with_suffix(".pub").exists())
                self.assertFalse(authority.clone_root.exists())

    def test_pair_mismatch_is_detected_and_only_new_pair_is_removed(self):
        with self.provider() as provider:
            first = self.authority_at(provider)
            second = self.authority_at(provider)
            try:
                provider._prepare_profile_authority(first)
                public = provider._session_public_key(first).split()[:2]
                real_run = provider._runner.run

                def substitute_public(argv, **kwargs):
                    completed = real_run(argv, **kwargs)
                    if "-q" in argv and completed.returncode == 0:
                        second.identity_file.with_suffix(".pub").write_text(
                            " ".join((*public, second.host_key_alias)) + "\n", encoding="ascii",
                        )
                    return completed

                with (
                    mock.patch.object(provider._runner, "run", side_effect=substitute_public),
                    self.assertRaisesRegex(h.CandidateHarnessError, "CANDIDATE_VM_SESSION_KEY_INVALID"),
                ):
                    provider._prepare_profile_authority(second)
                self.assertTrue(first.identity_file.exists())
                self.assertFalse(second.identity_file.exists())
                self.assertFalse(second.identity_file.with_suffix(".pub").exists())
            finally:
                provider._destroy_session_key(first)
                provider._destroy_session_key(second)

    def test_delete_denial_still_removes_other_file_and_reports_failure(self):
        with self.provider() as provider:
            authority = self.authority_at(provider)
            provider._prepare_profile_authority(authority)
            try:
                with h.hold_windows_private_file(authority.identity_file):
                    with self.assertRaisesRegex(h.CandidateHarnessError, "CANDIDATE_VM_SESSION_KEY_DELETION_FAILED"):
                        provider._destroy_session_key(authority)
                    self.assertTrue(authority.identity_file.exists())
                    self.assertFalse(authority.identity_file.with_suffix(".pub").exists())
            finally:
                provider._destroy_session_key(authority)

    def test_existing_namespace_and_escaping_path_never_launch_or_overwrite(self):
        with self.provider() as provider:
            authority = self.authority_at(provider)
            authority.ssh_root.mkdir(parents=True)
            authority.identity_file.write_bytes(b"existing disposable fixture")
            with mock.patch.object(provider._runner, "run") as run:
                with self.assertRaisesRegex(h.CandidateHarnessError, "CANDIDATE_VM_PROFILE_NAMESPACE_EXISTS"):
                    provider._prepare_profile_authority(authority)
                escaped = self.authority_at(provider, self.root / "outside-work-root")
                with self.assertRaisesRegex(h.CandidateHarnessError, "CANDIDATE_VM_PROFILE_NAMESPACE_INVALID"):
                    provider._prepare_profile_authority(escaped)
                run.assert_not_called()
            self.assertEqual(authority.identity_file.read_bytes(), b"existing disposable fixture")
            provider._destroy_session_key(authority)

    def test_real_reparse_and_untrusted_mutation_acl_are_rejected_before_launch(self):
        with self.provider() as provider:
            work = provider._execution.work_root
            target = work / "junction-target"
            target.mkdir()
            link = work / "junction-link"
            # Fixed, task-owned ASCII paths; PowerShell creates only this link.
            subprocess.run([
                "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-NonInteractive",
                "-Command", f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null",
            ], check=True, capture_output=True, timeout=15)
            try:
                self.assertTrue(link.is_junction())
                authority = self.authority_at(provider, link / "session")
                with mock.patch.object(provider._runner, "run") as run:
                    with self.assertRaises((h.CandidateHarnessError, h.FormalWindowsPretrustError)):
                        provider._prepare_profile_authority(authority)
                    run.assert_not_called()
                self.assertFalse(authority.identity_file.exists())
            finally:
                link.rmdir()
            unsafe = work / "unsafe-fixture"
            unsafe.mkdir()
            subprocess.run([
                "C:/Windows/System32/icacls.exe", str(unsafe), "/grant", "*S-1-1-0:(OI)(CI)F",
            ], check=True, capture_output=True, timeout=15)
            authority = self.authority_at(provider, unsafe / "session")
            with mock.patch.object(provider._runner, "run") as run:
                with self.assertRaises((h.CandidateHarnessError, h.FormalWindowsPretrustError)):
                    provider._prepare_profile_authority(authority)
                run.assert_not_called()
            self.assertFalse(authority.identity_file.exists())

    def test_changed_private_tool_identity_is_rejected_before_launch(self):
        with self.provider() as provider:
            authority = self.authority_at(provider)
            original = provider._execution.tool_paths[h.SSH_KEYGEN]
            changed = self.root / "changed-keygen.exe"
            shutil.copyfile(original, changed)
            with changed.open("ab") as output:
                output.write(b"synthetic identity change")
            provider._execution.tool_paths[h.SSH_KEYGEN] = changed
            try:
                with mock.patch.object(provider._runner, "run") as run:
                    with self.assertRaisesRegex(h.CandidateHarnessError, "CANDIDATE_VM_EXECUTION_TOOL_IDENTITY_MISMATCH"):
                        provider._prepare_profile_authority(authority)
                    run.assert_not_called()
                self.assertFalse(authority.identity_file.exists())
            finally:
                provider._execution.tool_paths[h.SSH_KEYGEN] = original


if __name__ == "__main__":
    unittest.main()
