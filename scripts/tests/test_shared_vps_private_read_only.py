from __future__ import annotations

import hashlib
import inspect
import json
import unittest

from release.candidate import canonical_json_bytes
from release.formal_acceptance_test_support import build_test_formal_acceptance
from scripts import shared_vps_private_read_only as shared_vps
from scripts.shared_vps_private_read_only import (
    SHARED_VPS_PROHIBITED_OPERATIONS,
    SharedVpsPrivateReadOnlyError,
    SharedVpsReadOnlyTransportObservation,
    SharedVpsRetryLedger,
    _issue_test_only_transport_capability,
    _verify_shared_vps_private_read_only_test_only,
    acquire_shared_vps_private_key_lease,
    shared_vps_host_key_authority_identity,
    verify_shared_vps_private_read_only,
)


class SharedVpsPrivateReadOnlyTests(unittest.TestCase):
    HOST_KEY_FINGERPRINT = "SHA256:" + "A" * 43

    def setUp(self):
        self._controller_lifetimes = {}

    @staticmethod
    def formal_acceptance(*, rc_tag: str = "v1.1.0-rc.19") -> dict[str, object]:
        return build_test_formal_acceptance(
            rc_tag=rc_tag,
            rc_commit="1" * 40,
            rc_tree="2" * 40,
            release_manifest_identity="sha256:" + "3" * 64,
            deployment_contract_identity="sha256:" + "4" * 64,
            installer_materials_identity="sha256:" + "5" * 64,
            api_digest="sha256:" + "6" * 64,
            web_digest="sha256:" + "7" * 64,
            fresh_base_identity="sha256:" + "8" * 64,
            docker_base_identity="sha256:" + "9" * 64,
            runtime_base_identity="sha256:" + "a" * 64,
            accepted_at="2026-08-31T00:00:00Z",
            operator_identity="formal-reviewer",
        )

    @staticmethod
    def remote_files(formal: dict[str, object]) -> dict[str, bytes]:
        execution_receipt = formal["execution_receipt"]
        authority = canonical_json_bytes(
            {
                "api_digest": formal["api_digest"],
                "deployment_contract_identity": formal["deployment_contract_identity"],
                "formal_acceptance_identity": formal["identity"],
                "formal_aggregate_receipt_digest": execution_receipt[
                    "formal_aggregate_receipt_digest"
                ],
                "formal_execution_receipt_digest": execution_receipt[
                    "formal_execution_receipt_digest"
                ],
                "installer_materials_identity": formal["installer_materials_identity"],
                "publication_identity": formal["formal_evidence"][
                    "rcLiveAcceptanceInput"
                ]["publication_identity"],
                "rc_tag": formal["rc_tag"],
                "release_manifest_identity": formal["release_manifest_identity"],
                "schema": "animemo.shared-vps-release-authority/v2",
                "web_digest": formal["web_digest"],
            }
        )
        return {
            "SHA256SUMS": (
                hashlib.sha256(authority).hexdigest() + "  shared-vps-authority.json\n"
            ).encode("ascii"),
            "shared-vps-authority.json": authority,
        }

    @classmethod
    def host_key_authority_identity(cls) -> str:
        return shared_vps_host_key_authority_identity(
            host="45.207.221.83",
            port=2233,
            ssh_user="animemo-acceptance-ro",
            host_key_algorithm="ssh-ed25519",
            host_key_fingerprint=cls.HOST_KEY_FINGERPRINT,
        )

    @classmethod
    def immutable_plan_authority(cls, **overrides):
        values = {
            "schema": "animemo.shared-vps-immutable-plan-authority/v1",
            "host_key_authority_identity": cls.host_key_authority_identity(),
            "transport_authority_identity": "sha256:" + "c" * 64,
            "helper_binary_identity": "sha256:" + "d" * 64,
            "forced_command_policy_identity": "sha256:" + "e" * 64,
        }
        values.update(overrides)
        identity = "sha256:" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()
        return shared_vps.SharedVpsImmutablePlanAuthority(
            **values,
            identity=identity,
        )

    def controller_lifetime(self, immutable_plan_authority):
        authority = self._controller_lifetimes.get(immutable_plan_authority.identity)
        if authority is None:
            authority = shared_vps._issue_test_only_controller_lifetime_authority(
                immutable_plan_authority
            )
            self._controller_lifetimes[immutable_plan_authority.identity] = authority
        return authority

    def capability(self, transport, immutable_plan_authority=None):
        if transport is None:
            return None
        plan = (
            SharedVpsPrivateReadOnlyTests.immutable_plan_authority()
            if immutable_plan_authority is None
            else immutable_plan_authority
        )
        return _issue_test_only_transport_capability(
            transport,
            immutable_plan_authority=plan,
            controller_lifetime_authority=self.controller_lifetime(plan),
        )

    @staticmethod
    def observation(request, files, **overrides):
        values = {
            "host": request.host,
            "port": request.port,
            "ssh_user": request.ssh_user,
            "host_key_algorithm": request.host_key_algorithm,
            "host_key_fingerprint": request.host_key_fingerprint,
            "host_key_authority_identity": request.host_key_authority_identity,
            "remote_command": request.remote_command,
            "transport_authority_identity": request.transport_authority_identity,
            "helper_binary_identity": request.helper_binary_identity,
            "forced_command_policy_identity": (request.forced_command_policy_identity),
            "resolved_read_only_path": request.allowed_read_only_path,
            "closed_inventory": request.allowed_files,
            "files": files,
            "connection_count": 1,
            "command_count": 1,
            "read_only_observation_count": 2,
            "mutation_count": 0,
            "v1_0_access_count": 0,
            "unrelated_site_access_count": 0,
            "dns_or_cloudflare_access_count": 0,
            "firewall_access_count": 0,
            "openresty_access_count": 0,
            "regular_file_count": 2,
            "symlink_count": 0,
            "path_escape_count": 0,
        }
        values.update(overrides)
        return SharedVpsReadOnlyTransportObservation(**values)

    def verify(
        self,
        *,
        formal: dict[str, object],
        credential,
        transport,
        **changes,
    ):
        immutable_plan_authority = changes.pop(
            "immutable_plan_authority",
            self.immutable_plan_authority(),
        )
        values = {
            "formal_acceptance_record": formal,
            "host": "45.207.221.83",
            "port": 2233,
            "access": "PRIVATE_READ_ONLY",
            "allowed_read_only_path": "/opt/animemo-v1.1/acceptance",
            "ssh_user": "animemo-acceptance-ro",
            "host_key_algorithm": "ssh-ed25519",
            "host_key_fingerprint": self.HOST_KEY_FINGERPRINT,
            "host_key_authority_identity": (self.host_key_authority_identity()),
            "immutable_plan_authority": immutable_plan_authority,
            "credential": credential,
            "prohibited_operations": SHARED_VPS_PROHIBITED_OPERATIONS,
            "transport_capability": (
                self.capability(
                    transport,
                    immutable_plan_authority,
                )
            ),
        }
        values.update(changes)
        return _verify_shared_vps_private_read_only_test_only(**values)

    def test_valid_formal_acceptance_authorizes_one_closed_read_only_probe(self):
        formal = self.formal_acceptance()
        files = self.remote_files(formal)

        class Transport:
            calls = 0

            def observe(self, request, credential):
                self.calls += 1
                self.request = request
                self.private_key_length = len(credential.read_once())
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    files,
                )

        transport = Transport()
        plan = self.immutable_plan_authority()
        capability = self.capability(transport, plan)
        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        receipt = _verify_shared_vps_private_read_only_test_only(
            formal_acceptance_record=formal,
            host="45.207.221.83",
            port=2233,
            access="PRIVATE_READ_ONLY",
            allowed_read_only_path="/opt/animemo-v1.1/acceptance",
            ssh_user="animemo-acceptance-ro",
            host_key_algorithm="ssh-ed25519",
            host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
            host_key_authority_identity=self.host_key_authority_identity(),
            immutable_plan_authority=plan,
            credential=credential,
            prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
            transport_capability=capability,
        )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(transport.private_key_length, 33)
        self.assertEqual(
            transport.request.allowed_files,
            ("SHA256SUMS", "shared-vps-authority.json"),
        )
        self.assertEqual(
            transport.request.prohibited_operations,
            SHARED_VPS_PROHIBITED_OPERATIONS,
        )
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(receipt["formalAcceptanceIdentity"], formal["identity"])
        self.assertEqual(
            receipt["formalAggregateReceiptDigest"],
            formal["execution_receipt"]["formal_aggregate_receipt_digest"],
        )
        self.assertEqual(
            receipt["formalExecutionReceiptDigest"],
            formal["execution_receipt"]["formal_execution_receipt_digest"],
        )
        self.assertEqual(receipt["readOnlyObservationCount"], 2)
        self.assertEqual(receipt["mutationCount"], 0)
        self.assertEqual(
            receipt["controllerLifetimeIssuanceIdentity"],
            capability.controller_lifetime_issuance_identity,
        )
        self.assertEqual(
            receipt["probeAuthorityIdentity"],
            shared_vps._probe_authority_identity(
                formal=formal,
                immutable_plan_authority=plan,
                controller_lifetime_issuance_identity=(
                    capability.controller_lifetime_issuance_identity
                ),
            ),
        )
        self.assertTrue(credential.cleared)

    def test_public_production_verifier_rejects_test_only_capability(self):
        formal = self.formal_acceptance()
        files = self.remote_files(formal)

        class Transport:
            calls = 0

            def observe(self, request, credential):
                self.calls += 1
                credential.read_once()
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    files,
                )

        transport = Transport()
        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_TEST_AUTHORITY_FORBIDDEN",
        ):
            verify_shared_vps_private_read_only(
                formal_acceptance_record=formal,
                host="45.207.221.83",
                port=2233,
                access="PRIVATE_READ_ONLY",
                allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                ssh_user="animemo-acceptance-ro",
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                host_key_authority_identity=self.host_key_authority_identity(),
                immutable_plan_authority=self.immutable_plan_authority(),
                credential=credential,
                prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                transport_capability=self.capability(transport),
            )
        self.assertEqual(0, transport.calls)
        self.assertTrue(credential.cleared)

    def test_external_immutable_plan_prefixes_all_four_probe_authorities(self):
        formal = self.formal_acceptance()
        base_plan = self.immutable_plan_authority()

        class Transport:
            calls = 0

            def observe(self, request, credential):
                del request, credential
                self.calls += 1
                raise AssertionError("plan mismatch must fail before transport")

        transport = Transport()
        capability = _issue_test_only_transport_capability(
            transport,
            immutable_plan_authority=base_plan,
            controller_lifetime_authority=self.controller_lifetime(base_plan),
        )
        base_probe_identity = shared_vps._probe_authority_identity(
            formal=formal,
            immutable_plan_authority=base_plan,
            controller_lifetime_issuance_identity=(
                capability.controller_lifetime_issuance_identity
            ),
        )
        mutations = {
            "host_key_authority_identity": "sha256:" + "1" * 64,
            "transport_authority_identity": "sha256:" + "2" * 64,
            "helper_binary_identity": "sha256:" + "3" * 64,
            "forced_command_policy_identity": "sha256:" + "4" * 64,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed_plan = self.immutable_plan_authority(**{field: value})
                changed_probe_identity = shared_vps._probe_authority_identity(
                    formal=formal,
                    immutable_plan_authority=changed_plan,
                    controller_lifetime_issuance_identity=(
                        capability.controller_lifetime_issuance_identity
                    ),
                )
                self.assertNotEqual(base_probe_identity, changed_probe_identity)

                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaisesRegex(
                    SharedVpsPrivateReadOnlyError,
                    "SHARED_VPS_IMMUTABLE_PLAN_AUTHORITY_MISMATCH",
                ):
                    _verify_shared_vps_private_read_only_test_only(
                        formal_acceptance_record=formal,
                        host="45.207.221.83",
                        port=2233,
                        access="PRIVATE_READ_ONLY",
                        allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                        ssh_user="animemo-acceptance-ro",
                        host_key_algorithm="ssh-ed25519",
                        host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                        host_key_authority_identity=(
                            self.host_key_authority_identity()
                        ),
                        immutable_plan_authority=changed_plan,
                        credential=credential,
                        prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                        transport_capability=capability,
                    )
                self.assertTrue(credential.cleared)
        self.assertEqual(0, transport.calls)

    def test_probe_before_formal_pass_is_rejected_without_transport_or_secret_residue(
        self,
    ):
        class Transport:
            calls = 0

            def observe(self, request, credential):
                del request, credential
                self.calls += 1
                raise AssertionError("transport must not run before Formal PASS")

        transport = Transport()
        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID",
        ):
            verify_shared_vps_private_read_only(
                formal_acceptance_record=None,
                host="45.207.221.83",
                port=2233,
                access="PRIVATE_READ_ONLY",
                allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                ssh_user="animemo-acceptance-ro",
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                host_key_authority_identity=self.host_key_authority_identity(),
                immutable_plan_authority=self.immutable_plan_authority(),
                credential=credential,
                prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                transport_capability=self.capability(transport),
            )
        self.assertEqual(transport.calls, 0)
        self.assertTrue(credential.cleared)

    def test_formal_vm_harness_exports_the_production_verifier_symbol(self):
        from scripts import formal_vm_harness

        self.assertIs(
            formal_vm_harness.verify_shared_vps_private_read_only,
            verify_shared_vps_private_read_only,
        )

    def test_managed_lease_view_is_zeroed_after_probe_returns(self):
        formal = self.formal_acceptance()
        files = self.remote_files(formal)

        class Transport:
            retained_view = None

            def observe(self, request, credential):
                self.retained_view = credential.read_once()
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    files,
                )

        transport = Transport()
        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        _verify_shared_vps_private_read_only_test_only(
            formal_acceptance_record=formal,
            host="45.207.221.83",
            port=2233,
            access="PRIVATE_READ_ONLY",
            allowed_read_only_path="/opt/animemo-v1.1/acceptance",
            ssh_user="animemo-acceptance-ro",
            host_key_algorithm="ssh-ed25519",
            host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
            host_key_authority_identity=self.host_key_authority_identity(),
            immutable_plan_authority=self.immutable_plan_authority(),
            credential=credential,
            prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
            transport_capability=self.capability(transport),
        )
        self.assertTrue(credential.cleared)
        self.assertTrue(all(value == 0 for value in transport.retained_view))

    def test_valid_formal_record_from_another_rc_cannot_authorize_rc19_probe(self):
        class Transport:
            calls = 0

            def observe(self, request, credential):
                del request, credential
                self.calls += 1
                raise AssertionError("cross-RC authority must fail before transport")

        transport = Transport()
        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_RELEASE_IDENTITY_INVALID",
        ):
            _verify_shared_vps_private_read_only_test_only(
                formal_acceptance_record=self.formal_acceptance(rc_tag="v1.1.0-rc.18"),
                host="45.207.221.83",
                port=2233,
                access="PRIVATE_READ_ONLY",
                allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                ssh_user="animemo-acceptance-ro",
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                host_key_authority_identity=self.host_key_authority_identity(),
                immutable_plan_authority=self.immutable_plan_authority(),
                credential=credential,
                prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                transport_capability=self.capability(transport),
            )
        self.assertEqual(transport.calls, 0)
        self.assertTrue(credential.cleared)

    def test_broad_or_ambient_ssh_principal_is_rejected_before_transport(self):
        class Transport:
            calls = 0

            def observe(self, request, credential):
                del request, credential
                self.calls += 1
                raise AssertionError("non-dedicated SSH user must not reach transport")

        transport = Transport()
        for ssh_user in ("root", "ubuntu", "admin"):
            with self.subTest(ssh_user=ssh_user):
                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaisesRegex(
                    SharedVpsPrivateReadOnlyError,
                    "SHARED_VPS_SSH_USER_INVALID",
                ):
                    _verify_shared_vps_private_read_only_test_only(
                        formal_acceptance_record=self.formal_acceptance(),
                        host="45.207.221.83",
                        port=2233,
                        access="PRIVATE_READ_ONLY",
                        allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                        ssh_user=ssh_user,
                        host_key_algorithm="ssh-ed25519",
                        host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                        host_key_authority_identity=(
                            self.host_key_authority_identity()
                        ),
                        immutable_plan_authority=self.immutable_plan_authority(),
                        credential=credential,
                        prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                        transport_capability=self.capability(transport),
                    )
                self.assertTrue(credential.cleared)
        self.assertEqual(transport.calls, 0)

    def test_transport_failure_exposes_only_a_fixed_code_and_clears_secret(self):
        class Transport:
            def observe(self, request, credential):
                del request
                secret = bytes(credential.read_once()).decode("ascii")
                raise RuntimeError("transport leaked " + secret)

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaises(SharedVpsPrivateReadOnlyError) as captured:
            _verify_shared_vps_private_read_only_test_only(
                formal_acceptance_record=self.formal_acceptance(),
                host="45.207.221.83",
                port=2233,
                access="PRIVATE_READ_ONLY",
                allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                ssh_user="animemo-acceptance-ro",
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                host_key_authority_identity=self.host_key_authority_identity(),
                immutable_plan_authority=self.immutable_plan_authority(),
                credential=credential,
                prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                transport_capability=self.capability(Transport()),
            )
        self.assertEqual(captured.exception.code, "SHARED_VPS_TRANSPORT_FAILED")
        self.assertEqual(str(captured.exception), "SHARED_VPS_TRANSPORT_FAILED")
        self.assertIsNone(captured.exception.__cause__)
        self.assertTrue(credential.cleared)

    def test_base_exception_at_credential_consumption_boundary_is_fixed_and_cleared(
        self,
    ):
        for fatal in (
            KeyboardInterrupt("synthetic-memory-only-private-key"),
            SystemExit("synthetic-memory-only-private-key"),
        ):
            with self.subTest(fatal=type(fatal).__name__):

                class FatalTransport:
                    def observe(self, request, credential, _fatal=fatal):
                        del request
                        credential.read_once()
                        raise _fatal

                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaises(SharedVpsPrivateReadOnlyError) as captured:
                    self.verify(
                        formal=self.formal_acceptance(),
                        credential=credential,
                        transport=FatalTransport(),
                    )
                self.assertEqual(
                    "SHARED_VPS_TRANSPORT_FAILED",
                    captured.exception.code,
                )
                self.assertEqual(
                    "SHARED_VPS_TRANSPORT_FAILED",
                    str(captured.exception),
                )
                self.assertIsNone(captured.exception.__cause__)
                self.assertTrue(credential.cleared)

        class SpoofedVerifierErrorTransport:
            def observe(self, request, credential):
                del request
                credential.read_once()
                raise SharedVpsPrivateReadOnlyError("synthetic-memory-only-private-key")

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaises(SharedVpsPrivateReadOnlyError) as captured:
            self.verify(
                formal=self.formal_acceptance(),
                credential=credential,
                transport=SpoofedVerifierErrorTransport(),
            )
        self.assertEqual(captured.exception.code, "SHARED_VPS_TRANSPORT_FAILED")
        self.assertIsNone(captured.exception.__cause__)
        self.assertTrue(credential.cleared)

    def test_each_independent_failure_root_allows_at_most_ten_ordered_retries(self):
        ledger = SharedVpsRetryLedger()
        probe_authority = "sha256:" + "1" * 64
        self.assertEqual(ledger.begin_probe_attempt(probe_authority), 0)
        first_root = ledger.record_probe_failure(
            probe_authority, "SHARED_VPS_TRANSPORT_FAILED"
        )
        for retry_attempt in range(1, 6):
            self.assertEqual(ledger.begin_probe_attempt(probe_authority), retry_attempt)
            self.assertEqual(
                ledger.record_probe_failure(
                    probe_authority, "SHARED_VPS_TRANSPORT_FAILED"
                ),
                first_root,
            )
        self.assertEqual(ledger.begin_probe_attempt(probe_authority), 6)
        second_root = ledger.record_probe_failure(
            probe_authority, "SHARED_VPS_REMOTE_AUTHORITY_INVALID"
        )
        self.assertNotEqual(first_root, second_root)
        for retry_attempt in range(1, 11):
            self.assertEqual(ledger.begin_probe_attempt(probe_authority), retry_attempt)
            self.assertEqual(
                ledger.record_probe_failure(
                    probe_authority, "SHARED_VPS_REMOTE_AUTHORITY_INVALID"
                ),
                second_root,
            )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_RETRY_LIMIT_EXHAUSTED",
        ):
            ledger.begin_probe_attempt(probe_authority)

    def test_public_api_cannot_replace_the_long_lived_retry_authority(self):
        self.assertNotIn(
            "retry_ledger",
            inspect.signature(verify_shared_vps_private_read_only).parameters,
        )
        self.assertNotIn(
            "retry_ledger",
            inspect.signature(
                _verify_shared_vps_private_read_only_test_only
            ).parameters,
        )

        class FailingTransport:
            calls = 0

            def observe(self, request, credential):
                del request
                credential.read_once()
                self.calls += 1
                raise RuntimeError("synthetic transport failure")

        transport = FailingTransport()
        plan = self.immutable_plan_authority()
        capability = self.capability(transport, plan)
        with self.assertRaises(AttributeError):
            capability._retry_ledger = SharedVpsRetryLedger()
        for _ in range(11):
            credential = acquire_shared_vps_private_key_lease(
                bytearray(b"synthetic-memory-only-private-key")
            )
            with self.assertRaisesRegex(
                SharedVpsPrivateReadOnlyError,
                "SHARED_VPS_TRANSPORT_FAILED",
            ):
                self.verify(
                    formal=self.formal_acceptance(),
                    credential=credential,
                    transport=None,
                    immutable_plan_authority=plan,
                    transport_capability=capability,
                )
            self.assertTrue(credential.cleared)

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_RETRY_LIMIT_EXHAUSTED",
        ):
            self.verify(
                formal=self.formal_acceptance(),
                credential=credential,
                transport=None,
                immutable_plan_authority=plan,
                transport_capability=capability,
            )
        self.assertEqual(11, transport.calls)
        self.assertTrue(credential.cleared)

    def test_rotating_two_same_plan_capabilities_cannot_reset_retry_authority(self):
        class FailingTransport:
            calls = 0

            def observe(self, request, credential):
                del request
                credential.read_once()
                self.calls += 1
                raise RuntimeError("synthetic transport failure")

        plan = self.immutable_plan_authority()
        lifetime = shared_vps._issue_test_only_controller_lifetime_authority(plan)
        other_lifetime = shared_vps._issue_test_only_controller_lifetime_authority(plan)
        self.assertNotEqual(
            lifetime.issuance_identity, other_lifetime.issuance_identity
        )
        with self.assertRaises(AttributeError):
            lifetime._retry_ledger = SharedVpsRetryLedger()
        first_transport = FailingTransport()
        second_transport = FailingTransport()
        capabilities = (
            _issue_test_only_transport_capability(
                first_transport,
                immutable_plan_authority=plan,
                controller_lifetime_authority=lifetime,
            ),
            _issue_test_only_transport_capability(
                second_transport,
                immutable_plan_authority=plan,
                controller_lifetime_authority=lifetime,
            ),
        )
        self.assertIsNot(capabilities[0], capabilities[1])
        self.assertEqual(
            capabilities[0].controller_lifetime_issuance_identity,
            capabilities[1].controller_lifetime_issuance_identity,
        )
        with self.assertRaises(AttributeError):
            capabilities[0]._controller_lifetime_authority = other_lifetime
        self.assertNotEqual(
            shared_vps._probe_authority_identity(
                formal=self.formal_acceptance(),
                immutable_plan_authority=plan,
                controller_lifetime_issuance_identity=lifetime.issuance_identity,
            ),
            shared_vps._probe_authority_identity(
                formal=self.formal_acceptance(),
                immutable_plan_authority=plan,
                controller_lifetime_issuance_identity=other_lifetime.issuance_identity,
            ),
        )
        for attempt in range(11):
            credential = acquire_shared_vps_private_key_lease(
                bytearray(b"synthetic-memory-only-private-key")
            )
            with self.assertRaisesRegex(
                SharedVpsPrivateReadOnlyError,
                "SHARED_VPS_TRANSPORT_FAILED",
            ):
                self.verify(
                    formal=self.formal_acceptance(),
                    credential=credential,
                    transport=None,
                    immutable_plan_authority=plan,
                    transport_capability=capabilities[attempt % 2],
                )
            self.assertTrue(credential.cleared)

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_RETRY_LIMIT_EXHAUSTED",
        ):
            self.verify(
                formal=self.formal_acceptance(),
                credential=credential,
                transport=None,
                immutable_plan_authority=plan,
                transport_capability=capabilities[1],
            )
        self.assertEqual(11, first_transport.calls + second_transport.calls)
        self.assertTrue(credential.cleared)
        self.assertNotIn(
            "controller_lifetime_authority",
            inspect.signature(verify_shared_vps_private_read_only).parameters,
        )

    def test_closed_file_set_and_integer_counts_are_not_type_coercible(self):
        formal = self.formal_acceptance()
        valid_files = self.remote_files(formal)

        def transport_with(*, files=None, **overrides):
            class Transport:
                def observe(self, request, credential):
                    credential.read_once()
                    return SharedVpsPrivateReadOnlyTests.observation(
                        request,
                        valid_files if files is None else files,
                        **overrides,
                    )

            return Transport()

        cases = (
            (
                "extra-file",
                transport_with(files={**valid_files, "other-site.json": b"{}\n"}),
                "SHARED_VPS_REMOTE_FILE_SET_INVALID",
            ),
            (
                "missing-file",
                transport_with(
                    files={
                        "shared-vps-authority.json": valid_files[
                            "shared-vps-authority.json"
                        ]
                    }
                ),
                "SHARED_VPS_REMOTE_FILE_SET_INVALID",
            ),
            (
                "bool-connection-count",
                transport_with(connection_count=True),
                "SHARED_VPS_TRANSPORT_COUNTS_INVALID",
            ),
            (
                "bool-zero-count",
                transport_with(mutation_count=False),
                "SHARED_VPS_TRANSPORT_COUNTS_INVALID",
            ),
            (
                "symlink-not-regular-file",
                transport_with(regular_file_count=1, symlink_count=1),
                "SHARED_VPS_TRANSPORT_COUNTS_INVALID",
            ),
            (
                "resolved-path-escape",
                transport_with(resolved_read_only_path="/opt/other-site"),
                "SHARED_VPS_REMOTE_PATH_AUTHORITY_INVALID",
            ),
        )
        for name, transport, code in cases:
            with self.subTest(name=name):
                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaisesRegex(SharedVpsPrivateReadOnlyError, code):
                    self.verify(
                        formal=formal,
                        credential=credential,
                        transport=transport,
                    )
                self.assertTrue(credential.cleared)

    def test_observation_identity_fields_reject_custom_equality_objects(self):
        formal = self.formal_acceptance()
        valid_files = self.remote_files(formal)

        class EqualToAnything:
            def __eq__(self, other):
                del other
                return True

        for field in (
            "host",
            "port",
            "resolved_read_only_path",
            "closed_inventory",
        ):
            with self.subTest(field=field):

                class Transport:
                    def observe(self, request, credential, _field=field):
                        credential.read_once()
                        return SharedVpsPrivateReadOnlyTests.observation(
                            request,
                            valid_files,
                            **{_field: EqualToAnything()},
                        )

                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaisesRegex(
                    SharedVpsPrivateReadOnlyError,
                    "SHARED_VPS_TRANSPORT_OBSERVATION_INVALID",
                ):
                    self.verify(
                        formal=formal,
                        credential=credential,
                        transport=Transport(),
                    )
                self.assertTrue(credential.cleared)

    def test_entrypoint_and_remote_file_keys_require_exact_primitive_types(self):
        formal = self.formal_acceptance()
        valid_files = self.remote_files(formal)

        class StringSubclass(str):
            pass

        class EqualToAnything:
            def __eq__(self, other):
                del other
                return True

        class MaliciousEquivalentKey:
            def __init__(self, value):
                self.value = value

            def __hash__(self):
                return hash(self.value)

            def __eq__(self, other):
                return self.value == other

        class Transport:
            calls = 0

            def observe(self, request, credential):
                credential.read_once()
                self.calls += 1
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    valid_files,
                )

        endpoint_cases = (
            ("host", EqualToAnything(), "SHARED_VPS_ENDPOINT_INVALID"),
            (
                "access",
                StringSubclass("PRIVATE_READ_ONLY"),
                "SHARED_VPS_ENDPOINT_INVALID",
            ),
            (
                "allowed_read_only_path",
                StringSubclass("/opt/animemo-v1.1/acceptance"),
                "SHARED_VPS_ENDPOINT_INVALID",
            ),
            (
                "host_key_algorithm",
                StringSubclass("ssh-ed25519"),
                "SHARED_VPS_HOST_KEY_AUTHORITY_INVALID",
            ),
            (
                "prohibited_operations",
                tuple(
                    StringSubclass(value) for value in SHARED_VPS_PROHIBITED_OPERATIONS
                ),
                "SHARED_VPS_PROHIBITED_OPERATIONS_INVALID",
            ),
        )
        transport = Transport()
        for field, value, code in endpoint_cases:
            with self.subTest(field=field):
                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaisesRegex(SharedVpsPrivateReadOnlyError, code):
                    self.verify(
                        formal=formal,
                        credential=credential,
                        transport=transport,
                        **{field: value},
                    )
                self.assertTrue(credential.cleared)
        self.assertEqual(0, transport.calls)

        key_factories = (StringSubclass, MaliciousEquivalentKey)
        for key_factory in key_factories:
            with self.subTest(remote_file_key=key_factory.__name__):
                changed_files = {
                    key_factory(name): value for name, value in valid_files.items()
                }

                class FilesTransport:
                    def observe(self, request, credential, _files=changed_files):
                        credential.read_once()
                        return SharedVpsPrivateReadOnlyTests.observation(
                            request,
                            _files,
                        )

                credential = acquire_shared_vps_private_key_lease(
                    bytearray(b"synthetic-memory-only-private-key")
                )
                with self.assertRaisesRegex(
                    SharedVpsPrivateReadOnlyError,
                    "SHARED_VPS_REMOTE_FILE_SET_INVALID",
                ):
                    self.verify(
                        formal=formal,
                        credential=credential,
                        transport=FilesTransport(),
                    )
                self.assertTrue(credential.cleared)

    def test_observed_host_key_and_remote_release_identity_must_match(self):
        formal = self.formal_acceptance()
        valid_files = self.remote_files(formal)

        class WrongHostKeyTransport:
            def observe(self, request, credential):
                credential.read_once()
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    valid_files,
                    host_key_fingerprint="SHA256:" + "B" * 43,
                )

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_TRANSPORT_AUTHORITY_MISMATCH",
        ):
            self.verify(
                formal=formal,
                credential=credential,
                transport=WrongHostKeyTransport(),
            )
        self.assertTrue(credential.cleared)

    def test_remote_authority_rejects_execution_receipt_identity_as_digest(self):
        formal = self.formal_acceptance()
        valid_files = self.remote_files(formal)
        changed_authority = dict(json.loads(valid_files["shared-vps-authority.json"]))
        changed_authority["formal_execution_receipt_digest"] = formal[
            "execution_receipt"
        ]["identity"]
        changed_authority_bytes = canonical_json_bytes(changed_authority)
        changed_files = {
            "SHA256SUMS": (
                hashlib.sha256(changed_authority_bytes).hexdigest()
                + "  shared-vps-authority.json\n"
            ).encode("ascii"),
            "shared-vps-authority.json": changed_authority_bytes,
        }

        class Transport:
            def observe(self, request, credential):
                credential.read_once()
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    changed_files,
                )

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_REMOTE_AUTHORITY_INVALID",
        ):
            self.verify(
                formal=formal,
                credential=credential,
                transport=Transport(),
            )
        self.assertTrue(credential.cleared)

        changed_authority = dict(json.loads(valid_files["shared-vps-authority.json"]))
        changed_authority["api_digest"] = "sha256:" + "f" * 64
        changed_authority_bytes = canonical_json_bytes(changed_authority)
        changed_files = {
            "SHA256SUMS": (
                hashlib.sha256(changed_authority_bytes).hexdigest()
                + "  shared-vps-authority.json\n"
            ).encode("ascii"),
            "shared-vps-authority.json": changed_authority_bytes,
        }

        class WrongReleaseTransport:
            def observe(self, request, credential):
                credential.read_once()
                return SharedVpsPrivateReadOnlyTests.observation(
                    request,
                    changed_files,
                )

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_REMOTE_AUTHORITY_INVALID",
        ):
            self.verify(
                formal=formal,
                credential=credential,
                transport=WrongReleaseTransport(),
            )
        self.assertTrue(credential.cleared)

    def test_no_ambient_credential_mapping_or_unsafe_default_transport_exists(self):
        parameters = inspect.signature(verify_shared_vps_private_read_only).parameters
        self.assertIn("credential", parameters)
        self.assertNotIn("credential_environment", parameters)

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_MEMORY_ONLY_SSH_TRANSPORT_UNAVAILABLE",
        ):
            self.verify(
                formal=self.formal_acceptance(),
                credential=credential,
                transport=None,
            )
        self.assertTrue(credential.cleared)

    def test_raw_transport_and_unbound_host_key_are_not_trust_authorities(self):
        class RawTransport:
            calls = 0

            def observe(self, request, credential):
                del request, credential
                self.calls += 1
                raise AssertionError("raw transport must not be trusted")

        transport = RawTransport()
        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_HOST_KEY_AUTHORITY_INVALID",
        ):
            verify_shared_vps_private_read_only(
                formal_acceptance_record=self.formal_acceptance(),
                host="45.207.221.83",
                port=2233,
                access="PRIVATE_READ_ONLY",
                allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                ssh_user="animemo-acceptance-ro",
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint="SHA256:" + "A" * 43,
                host_key_authority_identity="sha256:" + "0" * 64,
                immutable_plan_authority=self.immutable_plan_authority(),
                credential=credential,
                prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                transport_capability=transport,
            )
        self.assertEqual(transport.calls, 0)
        self.assertTrue(credential.cleared)

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_TRANSPORT_CAPABILITY_INVALID",
        ):
            verify_shared_vps_private_read_only(
                formal_acceptance_record=self.formal_acceptance(),
                host="45.207.221.83",
                port=2233,
                access="PRIVATE_READ_ONLY",
                allowed_read_only_path="/opt/animemo-v1.1/acceptance",
                ssh_user="animemo-acceptance-ro",
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint=self.HOST_KEY_FINGERPRINT,
                host_key_authority_identity=self.host_key_authority_identity(),
                immutable_plan_authority=self.immutable_plan_authority(),
                credential=credential,
                prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
                transport_capability=transport,
            )
        self.assertEqual(transport.calls, 0)
        self.assertTrue(credential.cleared)

        parameters = inspect.signature(
            shared_vps.verify_shared_vps_private_read_only
        ).parameters
        self.assertNotIn("transport", parameters)
        self.assertIn("transport_capability", parameters)
        self.assertTrue(hasattr(shared_vps, "_issue_test_only_transport_capability"))

    def test_probe_execution_enforces_ten_retries_for_derived_failure_root(self):
        class FailingTransport:
            calls = 0

            def observe(self, request, credential):
                del request
                credential.read_once()
                self.calls += 1
                raise RuntimeError("synthetic transport failure")

        transport = FailingTransport()
        plan = self.immutable_plan_authority()
        capability = self.capability(transport, plan)
        for _ in range(11):
            credential = acquire_shared_vps_private_key_lease(
                bytearray(b"synthetic-memory-only-private-key")
            )
            with self.assertRaisesRegex(
                SharedVpsPrivateReadOnlyError,
                "SHARED_VPS_TRANSPORT_FAILED",
            ):
                self.verify(
                    formal=self.formal_acceptance(),
                    credential=credential,
                    transport=None,
                    immutable_plan_authority=plan,
                    transport_capability=capability,
                )
            self.assertTrue(credential.cleared)

        credential = acquire_shared_vps_private_key_lease(
            bytearray(b"synthetic-memory-only-private-key")
        )
        with self.assertRaisesRegex(
            SharedVpsPrivateReadOnlyError,
            "SHARED_VPS_RETRY_LIMIT_EXHAUSTED",
        ):
            self.verify(
                formal=self.formal_acceptance(),
                credential=credential,
                transport=None,
                immutable_plan_authority=plan,
                transport_capability=capability,
            )
        self.assertEqual(transport.calls, 11)
        self.assertTrue(credential.cleared)


if __name__ == "__main__":
    unittest.main(verbosity=2)
