from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.candidate import canonical_json_bytes, sha256_bytes
from scripts import candidate_vm_harness as harness

DIGEST = "sha256:" + "a" * 64
SHA = "b" * 40
TREE = "c" * 40
RUN_ID = 1234


def _loaded(root: Path):
    return SimpleNamespace(
        root=root,
        verified_digest="sha256:" + "1" * 64,
        verified={"candidate_input_sha256": "sha256:" + "2" * 64},
        candidate_input={
            "qualification_run_id": RUN_ID,
            "qualification_run_attempt": 1,
            "source_sha": SHA,
            "source_tree": TREE,
            "candidate_version": "v1.1.0-rc.14",
        },
    )


class FakeProvider:
    def __init__(self):
        self.execute_calls = 0
        self.external_calls = 0
        self.external_state = dict(harness.EXPECTED_RC14_EXTERNAL_STATE)
        self.hashes = {
            name: "sha256:" + "3" * 64 for name in harness.SOURCE_VM_HASH_FILES
        }
        self.snapshots = {
            profile: "sha256:" + character * 64
            for profile, character in zip(harness.PROFILES, "456", strict=True)
        }

    def inspect_source(self):
        return harness.SourceVmEvidence(
            vm_identity=harness.SOURCE_VM_IDENTITY,
            snapshot_identities=self.snapshots,
            original_hashes=self.hashes,
        )

    def execute_profile(
        self, *, plan, harness_plan, candidate_root, initial_platform_state
    ):
        del candidate_root
        self.execute_calls += 1
        return {
            "schema": "animemo.prepublication-candidate-profile-receipt/v1",
            "version": 1,
            "candidate_input_digest": harness_plan.candidate_input_digest,
            "verified_candidate_digest": harness_plan.verified_candidate_digest,
            "qualification_run_id": harness_plan.qualification_run_id,
            "qualification_run_attempt": 1,
            "source_sha": harness_plan.source_sha,
            "source_tree": harness_plan.source_tree,
            "candidate_version": harness_plan.candidate_version,
            "profile": plan.profile,
            "base_vm_identity": harness_plan.source_vm_digest,
            "snapshot_identity": plan.snapshot_identity,
            "clone_identity": plan.clone_identity,
            "initial_platform_state": dict(initial_platform_state),
            "platform_bootstrap_plan_digest": "sha256:" + "7" * 64,
            "platform_bootstrap_receipt_digest": "sha256:" + "8" * 64,
            "strict_platform_qualification": True,
            "instance_mutation_before_platform_qualification": 0,
            "installer_plan_digest": "sha256:" + "9" * 64,
            "installer_execution_result": "PASS",
            "api_digest": DIGEST,
            "web_digest": DIGEST,
            "postgres_digest": DIGEST,
            "redis_digest": DIGEST,
            "doctor_result": "PASS",
            "canonical_test_results": [{"name": "acceptance", "result": "PASS"}],
            "network_request_count": 0,
            "apt_command_count": 0,
            "external_pull_count": 0,
            "original_vm_pre_hashes": dict(self.hashes),
            "original_vm_post_hashes": dict(self.hashes),
            "release_authority_granted": False,
            "publish_authorized": False,
            "started_at": "2026-08-25T12:00:00Z",
            "completed_at": "2026-08-25T12:01:00Z",
            "result": "PASS",
        }

    def inspect_original_hashes(self):
        return dict(self.hashes)

    def inspect_rc14_external_state(self):
        self.external_calls += 1
        return dict(self.external_state)


class PublicTransport:
    def __init__(self):
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url.startswith("https://ghcr.io/token?"):
            return 200, b'{"token":"anonymous-read-token"}'
        return 404, b"{}"


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, environment, input_bytes=None, timeout=300):
        self.calls.append(
            {
                "argv": tuple(argv),
                "environment": dict(environment),
                "input_bytes": input_bytes,
                "timeout": timeout,
            }
        )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class R2Transport:
    def get(self, url, headers):
        del headers
        if "?" in url:
            return 200, (
                b'{"result":[],"result_info":{"is_truncated":false},'
                b'"success":true}'
            )
        return 404, b"{}"


class CandidateVmHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.loaded = _loaded(self.root)
        self.provider = FakeProvider()

    def tearDown(self):
        self.temporary.cleanup()

    def _plan(self):
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ):
            return harness.build_harness_plan(
                verified_candidate_digest=self.loaded.verified_digest,
                expected_qualification_run_id=RUN_ID,
                expected_source_sha=SHA,
                expected_source_tree=TREE,
                provider=self.provider,
            )

    def test_default_mode_only_plans_all_three_fixed_profiles(self):
        plan = self._plan()
        self.assertEqual(tuple(item.profile for item in plan.profiles), harness.PROFILES)
        self.assertEqual(
            tuple(item.snapshot_name for item in plan.profiles),
            tuple(harness.SNAPSHOT_ALLOWLIST[item] for item in harness.PROFILES),
        )
        self.assertEqual(self.provider.execute_calls, 0)
        self.assertRegex(plan.plan_digest, r"^sha256:[0-9a-f]{64}$")

    def test_cli_exposes_no_vm_snapshot_shell_or_package_override(self):
        help_text = harness._parser().format_help()
        for forbidden in ("--vm-path", "--snapshot", "--command", "--package"):
            self.assertNotIn(forbidden, help_text)
        self.assertIn("--execute", help_text)
        self.assertIn("--accept-plan-digest", help_text)

    def test_incomplete_original_vm_inventory_is_rejected(self):
        self.provider.hashes.pop(harness.SOURCE_VM_HASH_FILES[0])
        with self.assertRaisesRegex(
            harness.CandidateHarnessError, "SOURCE_IDENTITY_INVALID"
        ):
            self._plan()

    def test_execute_fails_before_clone_when_r2_origin_is_not_proven(self):
        plan = self._plan()
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "R2_READONLY_CREDENTIAL_UNAVAILABLE"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment={},
            )
        self.assertEqual(self.provider.execute_calls, 0)

    def test_three_receipts_close_one_aggregate_without_publication_authority(self):
        plan = self._plan()
        environment = {
            "ANIMEMO_R2_ACCOUNT_ID": "account",
            "ANIMEMO_R2_READONLY_API_TOKEN": "readonly-token",
            "ANIMEMO_R2_BUCKET": "animemo-release-mirror",
            "ANIMEMO_R2_EXACT_PREFIX": "yanyuhanyue/AniMemo/releases/download/v1.1.0-rc.14/",
        }
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.candidate.R2_ACCOUNT_ID_SHA256", sha256_bytes(b"account")
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_transport=R2Transport(),
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["profileReceipts"]), 3)
        aggregate = result["aggregateReceipt"]
        self.assertTrue(aggregate["all_profiles_pass"])
        self.assertFalse(aggregate["release_authority_granted"])
        self.assertFalse(aggregate["publish_authorized"])
        self.assertEqual(self.provider.external_calls, 2)
        self.assertEqual(
            aggregate["rc14_prestate"], harness.EXPECTED_RC14_EXTERNAL_STATE
        )
        self.assertEqual(
            aggregate["rc14_poststate"], harness.EXPECTED_RC14_EXTERNAL_STATE
        )
        unsigned = dict(aggregate)
        receipt_digest = unsigned.pop("receipt_digest")
        self.assertEqual(receipt_digest, sha256_bytes(canonical_json_bytes(unsigned)))

    def test_wrong_plan_digest_never_starts_a_profile(self):
        plan = self._plan()
        with self.assertRaisesRegex(
            harness.CandidateHarnessError, "PLAN_NOT_ACCEPTED"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=DIGEST,
                provider=self.provider,
                environment={},
            )
        self.assertEqual(self.provider.execute_calls, 0)

    def test_original_vm_hash_drift_stops_before_the_next_profile(self):
        plan = self._plan()
        environment = {
            "ANIMEMO_R2_ACCOUNT_ID": "account",
            "ANIMEMO_R2_READONLY_API_TOKEN": "readonly-token",
            "ANIMEMO_R2_BUCKET": "animemo-release-mirror",
            "ANIMEMO_R2_EXACT_PREFIX": "yanyuhanyue/AniMemo/releases/download/v1.1.0-rc.14/",
        }
        original_execute = self.provider.execute_profile

        def mutate_source(**kwargs):
            receipt = original_execute(**kwargs)
            self.provider.hashes[harness.SOURCE_VM_HASH_FILES[0]] = (
                "sha256:" + "f" * 64
            )
            receipt["original_vm_post_hashes"] = dict(self.provider.hashes)
            return receipt

        self.provider.execute_profile = mutate_source
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.candidate.R2_ACCOUNT_ID_SHA256", sha256_bytes(b"account")
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "PROFILE_SAFETY_MISMATCH"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_transport=R2Transport(),
            )
        self.assertEqual(self.provider.execute_calls, 1)

    def test_rc14_presence_fails_before_the_first_profile(self):
        plan = self._plan()
        self.provider.external_state["tag"] = "PRESENT"
        environment = {
            "ANIMEMO_R2_ACCOUNT_ID": "account",
            "ANIMEMO_R2_READONLY_API_TOKEN": "readonly-token",
            "ANIMEMO_R2_BUCKET": "animemo-release-mirror",
            "ANIMEMO_R2_EXACT_PREFIX": "yanyuhanyue/AniMemo/releases/download/v1.1.0-rc.14/",
        }
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.candidate.R2_ACCOUNT_ID_SHA256", sha256_bytes(b"account")
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "RC14_NOT_EMPTY"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_transport=R2Transport(),
            )
        self.assertEqual(self.provider.external_calls, 1)
        self.assertEqual(self.provider.execute_calls, 0)

    def test_public_external_readback_uses_only_fixed_get_endpoints(self):
        transport = PublicTransport()
        provider = harness.ClosedVmwareProvider(
            public_transport=transport,
            environment={},
        )
        self.assertEqual(
            provider.inspect_rc14_external_state(),
            harness.EXPECTED_RC14_EXTERNAL_STATE,
        )
        urls = [url for url, _ in transport.calls]
        self.assertEqual(sum(url.startswith("https://ghcr.io/token?") for url in urls), 2)
        self.assertEqual(
            sum("https://download.animemo.cc/" in url for url in urls),
            len(harness.R2_RC14_EXPECTED_KEYS),
        )
        self.assertTrue(
            all(
                url.startswith(
                    (
                        "https://api.github.com/",
                        "https://download.animemo.cc/",
                        "https://ghcr.io/",
                    )
                )
                for url in urls
            )
        )

    def test_closed_provider_has_a_complete_success_lifecycle(self):
        plan = self._plan()
        profile = plan.profiles[0]
        provider = harness.ClosedVmwareProvider(
            environment={harness.GUEST_SUDO_PASSWORD_ENV: "test-only-password"}
        )
        receipt = self.provider.execute_profile(
            plan=profile,
            harness_plan=plan,
            candidate_root=self.root,
            initial_platform_state=harness._initial_platform_state(profile.profile),
        )
        clone_root = self.root / "closed-clone"
        clone_vmx = clone_root / "Ubuntu 64 位.vmx"
        with mock.patch.object(provider, "_assert_tools"), mock.patch.object(
            provider, "_hashes", return_value=dict(plan.original_vm_hashes)
        ), mock.patch.object(
            provider, "_clone_full", return_value=(clone_root, clone_vmx)
        ), mock.patch.object(provider, "_revert_clone") as revert, mock.patch.object(
            provider, "_validate_clone_disk_graph"
        ), mock.patch.object(
            provider, "_start_clone"
        ) as start, mock.patch.object(provider, "_wait_for_ssh"), mock.patch.object(
            provider, "_stage_candidate", return_value="/fixed/candidate"
        ), mock.patch.object(
            provider, "_run_profile_guest", return_value=receipt
        ), mock.patch.object(provider, "_stop_clone") as stop, mock.patch.object(
            provider, "_remove_clone"
        ) as remove, mock.patch.object(provider, "_quarantine_clone") as quarantine:
            observed = provider.execute_profile(
                plan=profile,
                harness_plan=plan,
                candidate_root=self.root,
                initial_platform_state=harness._initial_platform_state(profile.profile),
            )
        self.assertEqual(observed, receipt)
        revert.assert_called_once_with(clone_vmx, profile.snapshot_name)
        start.assert_called_once_with(clone_vmx)
        stop.assert_called_once_with(clone_vmx)
        remove.assert_called_once_with(clone_root)
        quarantine.assert_not_called()

    def test_host_commands_receive_only_sanitized_environment_and_stdin_secret(self):
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            environment={
                "SystemRoot": "C:/Windows",
                "USERPROFILE": "C:/Users/tester",
                "PATH": "C:/attacker-controlled",
                harness.GUEST_SUDO_PASSWORD_ENV: "test-only-password",
                "ANIMEMO_R2_READONLY_API_TOKEN": "test-only-r2-token",
            },
        )
        password = provider._sudo_password()
        provider._ssh_checked(
            "sudo -S -p '' -- /usr/bin/true",
            code="TEST",
            sudo_password=password,
        )
        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        self.assertNotIn("test-only-password", call["argv"])
        self.assertNotIn("test-only-r2-token", call["argv"])
        self.assertEqual(call["input_bytes"], b"test-only-password\n")
        self.assertNotIn(harness.GUEST_SUDO_PASSWORD_ENV, call["environment"])
        self.assertNotIn(
            "ANIMEMO_R2_READONLY_API_TOKEN", call["environment"]
        )
        self.assertNotIn("test-only-password", call["environment"].values())
        self.assertNotIn("test-only-r2-token", call["environment"].values())
        self.assertNotIn("C:/attacker-controlled", call["environment"]["PATH"])
        self.assertEqual(call["environment"]["SYSTEMROOT"], "C:/Windows")

    def test_shared_writable_vmx_and_vmdk_references_are_rejected(self):
        outside = self.root / "shared.vmdk"
        outside.write_bytes(b"shared")
        fixtures = {
            "vmx-parent": (
                'scsi0:0.fileName = "../shared.vmdk"\n',
                b"# Disk DescriptorFile\nparentCID=ffffffff\n",
            ),
            "vmdk-parent": (
                'scsi0:0.fileName = "disk.vmdk"\n',
                b'# Disk DescriptorFile\nparentCID=00000001\n'
                b'parentFileNameHint="../shared.vmdk"\n',
            ),
        }
        for name, (vmx_text, descriptor) in fixtures.items():
            with self.subTest(name=name):
                clone = self.root / name
                clone.mkdir()
                vmx = clone / "Ubuntu 64 位.vmx"
                vmx.write_text(vmx_text, encoding="utf-8")
                (clone / "disk.vmdk").write_bytes(descriptor)
                with self.assertRaisesRegex(
                    harness.CandidateHarnessError, "SHARED_DISK_REJECTED"
                ):
                    harness.ClosedVmwareProvider._validate_clone_disk_graph(
                        clone, vmx
                    )

    def test_partial_start_failure_is_contained_before_quarantine(self):
        plan = self._plan()
        profile = plan.profiles[0]
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(runner=runner, environment={})
        clone_root = self.root / "partial-start"
        clone_vmx = clone_root / "Ubuntu 64 位.vmx"
        with mock.patch.object(provider, "_assert_tools"), mock.patch.object(
            provider, "_hashes", return_value=dict(plan.original_vm_hashes)
        ), mock.patch.object(
            provider, "_clone_full", return_value=(clone_root, clone_vmx)
        ), mock.patch.object(provider, "_revert_clone"), mock.patch.object(
            provider, "_validate_clone_disk_graph"
        ), mock.patch.object(
            provider,
            "_start_clone",
            side_effect=harness.CandidateHarnessError("CANDIDATE_VM_CLONE_START_FAILED"),
        ), mock.patch.object(
            provider,
            "_stop_clone",
            side_effect=harness.CandidateHarnessError(
                "CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED"
            ),
        ), mock.patch.object(
            provider, "_is_running", side_effect=[True] + [True] * 60 + [False]
        ), mock.patch(
            "scripts.candidate_vm_harness.time.sleep"
        ), mock.patch.object(
            provider, "_quarantine_clone"
        ) as quarantine, self.assertRaisesRegex(
            harness.CandidateHarnessError, "CLONE_START_FAILED"
        ):
            provider.execute_profile(
                plan=profile,
                harness_plan=plan,
                candidate_root=self.root,
                initial_platform_state=harness._initial_platform_state(profile.profile),
            )
        suspend_modes = [
            call["argv"][-1]
            for call in runner.calls
            if "suspend" in call["argv"]
        ]
        self.assertEqual(suspend_modes, ["soft", "hard"])
        quarantine.assert_called_once_with(clone_root, profile.clone_identity)

    def test_soft_shutdown_failure_uses_soft_suspend(self):
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(runner=runner, environment={})
        clone_vmx = self.root / "clone" / "Ubuntu 64 位.vmx"
        with mock.patch.object(
            provider,
            "_stop_clone",
            side_effect=harness.CandidateHarnessError(
                "CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED"
            ),
        ), mock.patch.object(provider, "_is_running", return_value=False):
            provider._contain_clone(clone_vmx)
        self.assertEqual(runner.calls[0]["argv"][3], "suspend")
        self.assertEqual(runner.calls[0]["argv"][-1], "soft")


if __name__ == "__main__":
    unittest.main()
