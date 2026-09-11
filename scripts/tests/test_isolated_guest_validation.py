from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_vm_harness as h
from scripts import isolated_guest_validation as entry
from scripts.tests import test_candidate_vm_harness as fixtures


class IsolatedGuestValidationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CandidateVmHarnessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.root = fixture.root
        self.plan, self.profile, self.authority, self.runtime, bootstrap, verified = fixture._connection_fixture()
        self.bootstrap = h.VerifiedCloneConnection(
            authority=self.authority, runtime=self.runtime, guest=bootstrap,
        )
        self.verified = h.VerifiedCloneConnection(
            authority=self.authority, runtime=self.runtime, guest=verified,
        )
        self.provider = h.ClosedVmwareProvider(
            runner=fixtures.RecordingRunner(), windows_platform=fixtures.FakeWindowsPlatform(),
            environment={},
        )
        # Synthetic authority observations, never a production verifier output.
        material = object.__new__(h.HeldCandidateMaterialAuthority)
        material._closed = False
        material._loaded = SimpleNamespace(verified_digest=self.plan.verified_candidate_digest)
        self.provider._candidate_material_authority = material
        self.provider._execution = SimpleNamespace(work_root=self.root)
        self.result = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def patch(self, target, name, **kwargs):
        return self.stack.enter_context(mock.patch.object(target, name, **kwargs))

    def clone_fixture(self, *, prepare_error=None, observation_error=None, containment="STOPPED"):
        self.patch(self.provider, "inspect_readiness", return_value=SimpleNamespace(
            receipt_digest=self.plan.provider_readiness_receipt_digest))
        self.patch(self.provider, "_assert_tools")
        self.patch(self.provider, "_hashes", return_value=dict(self.plan.original_vm_hashes))
        self.patch(self.provider, "_active_profile_authority", return_value=self.authority)
        self.patch(self.provider, "_acquire_provider_lease", return_value=mock.sentinel.lease)
        release = self.patch(self.provider, "_release_provider_lease")
        def prepare(**kwargs):
            self.assertEqual(kwargs["plan"].profile, "FRESH_BASE")
            self.authority.clone_root.mkdir(parents=True)
            self.authority.clone_vmx.write_bytes(b"synthetic VM bytes")
            self.authority.ssh_root.mkdir(parents=True)
            self.authority.identity_file.write_bytes(b"synthetic key")
            self.authority.identity_file.with_suffix(".pub").write_bytes(b"synthetic public key")
            self.authority.known_hosts_file.write_bytes(b"synthetic known host")
            if prepare_error:
                raise prepare_error
            return self.runtime.disk_graph_digest, self.runtime.snapshot_identity
        self.patch(self.provider, "_prepare_and_start_profile_clone", side_effect=prepare)
        observe = self.patch(self.provider, "_verify_bootstrap_connection",
            side_effect=observation_error, return_value=self.bootstrap)
        self.patch(self.provider, "_remove_known_hosts")
        self.patch(self.provider, "_running_vmx_paths", return_value=frozenset({
            os.path.normcase(str(self.authority.clone_vmx.resolve(strict=False)))}))
        contain = self.patch(self.provider, "_contain_clone", return_value=containment)
        return observe, contain, release

    def assert_retained_without_keys(self):
        self.assertEqual(self.authority.clone_vmx.read_bytes(), b"synthetic VM bytes")
        self.assertFalse(self.authority.identity_file.exists())
        self.assertFalse(self.authority.identity_file.with_suffix(".pub").exists())
        self.assertFalse(self.authority.known_hosts_file.exists())
        self.assertTrue(self.result["resource"]["session_keys_removed"])
        self.assertTrue(self.result["resource"]["lease_released"])

    def test_one_clone_is_observed_then_stopped_and_retained_without_keys(self):
        observe, contain, release = self.clone_fixture()
        with entry._controller_clone(self.provider, self.plan, self.result) as scope:
            self.assertEqual(scope[0], self.profile)
            self.assertTrue(self.result["snapshot_started"])
            observe.assert_called_once()
            contain.assert_not_called()
        contain.assert_called_once_with(self.authority.clone_vmx)
        release.assert_called_once_with(mock.sentinel.lease, work_root=self.root)
        self.assertEqual(self.result["resource"]["power_state"], "STOPPED")
        self.assert_retained_without_keys()

    def test_partial_start_failure_is_contained_without_capture_and_retains_bytes(self):
        observe, contain, release = self.clone_fixture(
            prepare_error=h.CandidateHarnessError("SYNTHETIC_START_FAILURE"))
        with self.assertRaisesRegex(h.CandidateHarnessError, "SYNTHETIC_START_FAILURE"):
            with entry._controller_clone(self.provider, self.plan, self.result):
                self.fail("Failed start must not yield credential scope")
        observe.assert_not_called()
        contain.assert_called_once()
        release.assert_called_once()
        self.assert_retained_without_keys()

    def test_wrong_guest_never_yields_capture_scope(self):
        _, contain, _ = self.clone_fixture(
            observation_error=h.CandidateHarnessError("CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH"))
        with self.assertRaisesRegex(h.CandidateHarnessError, "CONNECTION_IDENTITY_MISMATCH"):
            with entry._controller_clone(self.provider, self.plan, self.result):
                self.fail("Wrong guest must not yield credential scope")
        contain.assert_called_once()
        self.assert_retained_without_keys()

    def test_suspend_is_distinct_from_shutdown(self):
        self.clone_fixture(containment="SUSPENDED")
        with entry._controller_clone(self.provider, self.plan, self.result):
            pass
        self.assertEqual(self.result["resource"]["power_state"], "SUSPENDED")
        self.assert_retained_without_keys()

    def test_failed_containment_still_destroys_keys_releases_lease_and_keeps_vm(self):
        _, contain, release = self.clone_fixture()
        contain.side_effect = h.CandidateHarnessError("CANDIDATE_VM_CLONE_CONTAINMENT_FAILED")
        with self.assertRaisesRegex(h.CandidateHarnessError, "CONTAINMENT_FAILED"):
            with entry._controller_clone(self.provider, self.plan, self.result):
                pass
        release.assert_called_once()
        self.assertEqual(self.result["resource"]["power_state"], "CONTAINMENT_PENDING")
        self.assert_retained_without_keys()

    def test_hold_close_failure_does_not_skip_other_holds_or_key_cleanup(self):
        _, _, release = self.clone_fixture()
        prepare = self.provider._prepare_and_start_profile_clone.side_effect
        events = []
        def fail_close():
            events.append("failed_clone_close")
            raise OSError("synthetic held directory close failure")
        def with_failed_hold(**kwargs):
            value = prepare(**kwargs)
            kwargs["clone_authority_stack"].callback(fail_close)
            kwargs["profile_authority_stack"].callback(lambda: events.append("profile_closed"))
            return value
        self.provider._prepare_and_start_profile_clone.side_effect = with_failed_hold
        with self.assertRaisesRegex(entry.ControllerFailure, "GUEST_CLONE_CLEANUP_FAILED"):
            with entry._controller_clone(self.provider, self.plan, self.result):
                pass
        self.assertEqual(events, ["failed_clone_close", "profile_closed"])
        release.assert_called_once()
        self.assert_retained_without_keys()
        self.assertEqual(self.result["resource"]["cleanup_errors"][0]["step"], "clone_holds")

    def test_key_deletion_failure_does_not_skip_known_hosts_or_lease_cleanup(self):
        _, _, release = self.clone_fixture()
        self.patch(self.provider, "_destroy_session_key",
            side_effect=h.CandidateHarnessError("CANDIDATE_VM_SESSION_KEY_DELETION_FAILED"))
        with self.assertRaisesRegex(entry.ControllerFailure, "GUEST_CLONE_CLEANUP_FAILED"):
            with entry._controller_clone(self.provider, self.plan, self.result):
                pass
        release.assert_called_once()
        self.assertFalse(self.authority.known_hosts_file.exists())
        self.assertFalse(self.result["resource"]["session_keys_removed"])
        self.assertEqual(self.result["resource"]["cleanup_errors"][0]["step"], "session_key")

    def validation_fixture(self):
        events = []
        @contextmanager
        def clone(*_args):
            events.append("identity")
            try:
                yield self.profile, mock.sentinel.lease, self.runtime.disk_graph_digest, self.runtime.snapshot_identity
            finally:
                events.append("cleanup")
        self.patch(entry, "_controller_clone", side_effect=clone)
        self.patch(entry, "_check_checkout")
        origin = self.patch(entry, "_origin", side_effect=lambda _plan, role:
            events.append(role) or {"observation_id": role, "result": "EMPTY"})
        self.patch(h, "_read_expected_external_state", return_value={"github_tag": "EMPTY"})
        self.patch(entry, "_reserve_capture", side_effect=lambda: events.append("reserve"))
        secret = bytearray(b"synthetic-local-sentinel")
        console = mock.Mock()
        console.capture.side_effect = lambda: events.append("capture") or secret
        supervisor = mock.Mock()
        supervisor.delivery_attempts = {"BOOTSTRAP_ROTATION": 1, "VERIFIED_SUDO": 1}
        supervisor.delivery_completed = dict(supervisor.delivery_attempts)
        supervisor._verified = self.verified
        supervisor.bootstrap_rotation.side_effect = lambda: events.append("rotation")
        supervisor.validate_verified_guest.side_effect = lambda: events.append("validation")
        constructor = self.patch(entry, "SessionSupervisor", return_value=supervisor)
        return events, origin, secret, console, supervisor, constructor

    def test_fixed_two_stage_order_and_fresh_poststate_after_cleanup(self):
        events, _, secret, console, supervisor, _ = self.validation_fixture()
        entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(events, ["PRESTATE", "identity", "reserve", "capture", "rotation", "validation", "cleanup", "POSTSTATE"])
        self.assertEqual(secret, b"")
        console.capture.assert_called_once()
        supervisor.close.assert_called_once()
        self.assertEqual(self.result["capture_completed"], 1)
        self.assertEqual(self.result["delivery_completed"], supervisor.delivery_completed)
        self.assertNotIn("aggregateReceipt", self.result)

    def test_r2_failure_prevents_clone_and_capture(self):
        _, origin, _, console, supervisor, _ = self.validation_fixture()
        origin.side_effect = h.CandidateContractError("SYNTHETIC_R2_UNAVAILABLE")
        with self.assertRaises(h.CandidateContractError):
            entry._validate(self.plan, self.provider, console, self.result)
        console.capture.assert_not_called()
        supervisor.bootstrap_rotation.assert_not_called()
        entry._controller_clone.assert_not_called()

    def test_keygen_diagnostic_preserves_only_process_facts_before_capture(self):
        events, _, _, console, supervisor, _ = self.validation_fixture()
        error = h.SessionKeyCommandError(
            "CANDIDATE_VM_SESSION_KEY_GENERATION_FAILED", kind="NONZERO_EXIT",
            returncode=255, stdout_empty=False, stderr_empty=True,
        )
        error.stdout = b"synthetic-sensitive-output"
        entry._controller_clone.side_effect = error
        with self.assertRaises(h.SessionKeyCommandError):
            entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(self.result["operation_failure_diagnostic"], {
            "tool": "ssh-keygen", "kind": "NONZERO_EXIT", "returncode": 255,
            "timeout": False, "stdout_empty": False, "stderr_empty": True,
        })
        self.assertNotIn("synthetic-sensitive-output", str(self.result))
        self.assertEqual(events, ["PRESTATE", "POSTSTATE"])
        console.capture.assert_not_called()
        supervisor.bootstrap_rotation.assert_not_called()

    def test_unknown_error_cannot_add_untrusted_diagnostic_output(self):
        _, _, _, console, _, _ = self.validation_fixture()
        error = RuntimeError("synthetic-sensitive-output")
        error.public_diagnostic = lambda: {"secret": "synthetic-sensitive-output"}
        entry._controller_clone.side_effect = error
        with self.assertRaises(RuntimeError):
            entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(self.result["operation_failure_code"], "GUEST_VALIDATION_INTERRUPTED_OR_UNCLASSIFIED")
        self.assertNotIn("operation_failure_diagnostic", self.result)
        self.assertNotIn("synthetic-sensitive-output", str(self.result))
        console.capture.assert_not_called()

    def test_plugin_pre_and_post_use_same_explicit_channel_after_cleanup(self):
        events, origin, _, console, _, _ = self.validation_fixture()
        channel = mock.sentinel.plugin_channel
        origin.side_effect = lambda _plan, role, **kwargs: (
            self.assertIs(kwargs['plugin_origin'], channel), events.append(role),
            {'observation_id': role})[-1]
        entry._validate(self.plan, self.provider, console, self.result, plugin_origin=channel)
        self.assertEqual(events, ["PRESTATE", "identity", "reserve", "capture", "rotation", "validation", "cleanup", "POSTSTATE"])

    def test_plugin_poststate_failure_remains_terminal_after_guest_cleanup(self):
        events, origin, _, console, supervisor, _ = self.validation_fixture()
        origin.side_effect = [{'observation_id': 'synthetic-pre'}, entry.R2PluginOriginError('R2_PLUGIN_RESPONSE_TIMEOUT')]
        with self.assertRaisesRegex(entry.R2PluginOriginError, 'TIMEOUT'):
            entry._validate(self.plan, self.provider, console, self.result, plugin_origin=mock.sentinel.plugin)
        supervisor.close.assert_called_once()
        self.assertIn('cleanup', events)
        self.assertEqual(self.result['poststate_failure_code'], 'R2_PLUGIN_RESPONSE_TIMEOUT')

    def test_constructor_failure_wipes_caller_owned_buffer_and_does_not_recapture(self):
        events, _, secret, console, supervisor, constructor = self.validation_fixture()
        constructor.side_effect = entry.ControllerFailure("HELD_GUEST_SCOPE_REQUIRED")
        with self.assertRaisesRegex(entry.ControllerFailure, "HELD_GUEST_SCOPE_REQUIRED"):
            entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(secret, b"")
        console.capture.assert_called_once()
        supervisor.bootstrap_rotation.assert_not_called()
        self.assertEqual(events[-2:], ["cleanup", "POSTSTATE"])

    def test_rotation_failure_is_terminal_but_records_possible_delivery(self):
        _, _, secret, console, supervisor, _ = self.validation_fixture()
        supervisor.bootstrap_rotation.side_effect = entry.ControllerFailure("BOOTSTRAP_ROTATION_FAILED")
        supervisor.delivery_attempts = {"BOOTSTRAP_ROTATION": 1, "VERIFIED_SUDO": 0}
        supervisor.delivery_completed = {"BOOTSTRAP_ROTATION": 0, "VERIFIED_SUDO": 0}
        with self.assertRaisesRegex(entry.ControllerFailure, "BOOTSTRAP_ROTATION_FAILED"):
            entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(secret, b"")
        console.capture.assert_called_once()
        supervisor.validate_verified_guest.assert_not_called()
        self.assertEqual(self.result["delivery_attempts"]["BOOTSTRAP_ROTATION"], 1)
        self.assertEqual(self.result["delivery_completed"]["BOOTSTRAP_ROTATION"], 0)

    def test_checkout_drift_during_capture_prevents_any_delivery(self):
        _, _, secret, console, supervisor, _ = self.validation_fixture()
        entry._check_checkout.side_effect = [None, entry.ControllerFailure("GUEST_CONTROLLER_SOURCE_CHANGED")]
        with self.assertRaisesRegex(entry.ControllerFailure, "GUEST_CONTROLLER_SOURCE_CHANGED"):
            entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(secret, b"")
        supervisor.bootstrap_rotation.assert_not_called()
        console.capture.assert_called_once()

    def test_poststate_failure_does_not_erase_original_guest_failure(self):
        _, origin, secret, console, supervisor, _ = self.validation_fixture()
        origin.side_effect = [{"observation_id": "PRESTATE"}, h.CandidateContractError("SYNTHETIC_POSTSTATE_FAILURE")]
        supervisor.bootstrap_rotation.side_effect = entry.ControllerFailure("BOOTSTRAP_ROTATION_FAILED")
        with self.assertRaisesRegex(h.CandidateContractError, "SYNTHETIC_POSTSTATE_FAILURE"):
            entry._validate(self.plan, self.provider, console, self.result)
        self.assertEqual(secret, b"")
        self.assertEqual(self.result["operation_failure_code"], "BOOTSTRAP_ROTATION_FAILED")
        self.assertEqual(self.result["poststate_failure_code"], "SYNTHETIC_POSTSTATE_FAILURE")

    def test_one_shot_reservation_survives_a_second_call_without_task_id_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / entry.CAPTURE_LEDGER.name
            with mock.patch.object(entry, "CAPTURE_LEDGER", ledger):
                entry._reserve_capture()
                with self.assertRaisesRegex(entry.ControllerFailure, "GUEST_CAPTURE_ALREADY_ATTEMPTED"):
                    entry._reserve_capture()
                self.assertTrue(ledger.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows execution retention")
    def test_real_private_execution_cleanup_keeps_synthetic_vm_but_deletes_bootstrap_copy(self):
        source = self.root / "synthetic-source"
        source.mkdir()
        (source / "authority.txt").write_bytes(b"synthetic source")
        keys = h.create_windows_private_directory(self.root, prefix="synthetic-bootstrap")
        bootstrap = keys / "id_ed25519"
        bootstrap.write_bytes(b"synthetic bootstrap identity")
        provider = h.ClosedVmwareProvider()
        retained_root = None
        try:
            with mock.patch.multiple(h, SOURCE_VM_ROOT=source, OPENSSH_IDENTITY=bootstrap):
                with provider.execution_authority(_retain_controller_data=True):
                    retained_root = provider._execution.root
                    work = provider._execution.work_root
                    private_key = provider._execution.bootstrap_identity
                    (work / "synthetic-stopped-clone.vmx").write_bytes(b"synthetic clone")
                self.assertFalse(private_key.exists())
                self.assertEqual(list(retained_root.iterdir()), [work])
                self.assertEqual((work / "synthetic-stopped-clone.vmx").read_bytes(), b"synthetic clone")
        finally:
            if retained_root is not None:
                resolved = retained_root.resolve(strict=True)
                self.assertEqual(resolved.parent, Path("E:/"))
                self.assertTrue(resolved.name.startswith("animemo-provider-execution-"))
                shutil.rmtree(resolved)

    @unittest.skipUnless(os.name == "nt", "Windows execution retention failure")
    def test_tool_cleanup_failure_still_removes_bootstrap_copy_and_other_temporary_roots(self):
        source = self.root / "synthetic-source"
        source.mkdir()
        (source / "authority.txt").write_bytes(b"synthetic source")
        keys = h.create_windows_private_directory(self.root, prefix="synthetic-bootstrap")
        bootstrap = keys / "id_ed25519"
        bootstrap.write_bytes(b"synthetic bootstrap identity")
        provider = h.ClosedVmwareProvider()
        retained_root = None
        remove_tree = shutil.rmtree
        def fail_tools_only(path, *args, **kwargs):
            if Path(path).parent == retained_root and Path(path).name.startswith("system-tools-"):
                raise OSError("synthetic tool cleanup failure")
            return remove_tree(path, *args, **kwargs)
        try:
            with mock.patch.multiple(h, SOURCE_VM_ROOT=source, OPENSSH_IDENTITY=bootstrap):
                with mock.patch.object(h.shutil, "rmtree", side_effect=fail_tools_only):
                    with self.assertRaisesRegex(h.CandidateHarnessError, "EXECUTION_AUTHORITY_RELEASE_FAILED"):
                        with provider.execution_authority(_retain_controller_data=True):
                            retained_root = provider._execution.root
                            work = provider._execution.work_root
                            private_key = provider._execution.bootstrap_identity
                            (work / "synthetic-stopped-clone.vmx").write_bytes(b"synthetic clone")
                self.assertFalse(private_key.exists())
                self.assertEqual(len(list(retained_root.iterdir())), 2)
                self.assertTrue((work / "synthetic-stopped-clone.vmx").exists())
        finally:
            if retained_root is not None:
                resolved = retained_root.resolve(strict=True)
                self.assertEqual(resolved.parent, Path("E:/"))
                self.assertTrue(resolved.name.startswith("animemo-provider-execution-"))
                remove_tree(resolved)


if __name__ == "__main__":
    unittest.main()
