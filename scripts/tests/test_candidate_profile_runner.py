from __future__ import annotations

import base64
import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.candidate import canonical_json_bytes
from scripts import candidate_profile_runner as runner

DIGEST = "sha256:" + "a" * 64


def _identity(value: object) -> str:
    return runner.sha256_bytes(runner.canonical_identity_bytes(value))


def _context(profile: str = "RUNTIME_BASE_OFFLINE"):
    return {
        "base_vm_identity": "sha256:" + "1" * 64,
        "clone_identity": "sha256:" + "2" * 64,
        "initial_platform_state": {
            "docker_present": True,
            "runtime_dependencies_present": True,
            "network_allowed": profile != "RUNTIME_BASE_OFFLINE",
        },
        "original_vm_pre_hashes": {"base.vmx": "sha256:" + "3" * 64},
        "profile": profile,
        "snapshot_identity": "sha256:" + "4" * 64,
    }


def _encoded_context(profile: str = "RUNTIME_BASE_OFFLINE") -> str:
    return base64.urlsafe_b64encode(canonical_json_bytes(_context(profile))).decode().rstrip("=")


def _loaded(root: Path):
    images = tuple(
        SimpleNamespace(role=role, digest=DIGEST)
        for role in ("api", "postgres", "redis", "web")
    )
    return SimpleNamespace(
        root=root,
        verified_digest="sha256:" + "5" * 64,
        verified={"candidate_input_sha256": "sha256:" + "6" * 64},
        candidate_input={
            "qualification_run_id": 123,
            "qualification_run_attempt": 1,
            "source_sha": "b" * 40,
            "source_tree": "c" * 40,
            "candidate_version": "v1.1.0-rc.14",
        },
        manifest={},
        materials=SimpleNamespace(identity_digest="sha256:" + "6" * 64),
        images=SimpleNamespace(images=images),
    )


def _installer_output():
    output = {
        "platformPlan": {},
        "platformBootstrapReceipt": {
            "planDigest": "sha256:" + "7" * 64,
            "actions": [],
            "result": "PASS",
        },
        "strictPostProvisionQualification": True,
        "installerPlanDigest": "sha256:" + "8" * 64,
        "installerResult": {
            "outcome": "SUCCEEDED",
            "completedSteps": ["runtime.validate", "doctor.accept"],
        },
    }
    doctor_report = {
        "reportFormat": "animemo-doctor-report",
        "reportVersion": 1,
        "checkedAt": "2026-08-25T12:00:30Z",
        "instanceId": "12345678-1234-4678-9234-567812345678",
        "deploymentProfile": "animemo-standard-v1",
        "doctorIdentity": {"format": "animemo-doctor-runtime", "version": 1},
        "mode": "READ-ONLY",
        "overallStatus": "PASS",
        "checks": [
            {
                "checkId": "service.api.health",
                "status": "PASS",
                "code": "API_HEALTHY",
                "severity": "info",
                "summary": "API health",
                "evidenceClass": "runtime",
                "remediation": "",
                "checkedAt": "2026-08-25T12:00:30Z",
            }
        ],
        "compatibility": {"outcome": "compatible"},
    }
    canonical_acceptance = []
    for name, adapter in (
        (
            "application.journal-crud",
            "django-domain-service-transaction-rollback",
        ),
        ("service.api.health", "immutable-compose-api-health"),
        ("service.web.health", "immutable-compose-web-health"),
    ):
        evidence = {
            "adapter": adapter,
            "observationDigest": _identity({"name": name, "status": "PASS"}),
        }
        body = {"evidence": evidence, "name": name, "result": "PASS"}
        canonical_acceptance.append(
            {
                **body,
                "receiptDigest": _identity(body),
            }
        )
    image_receipt = {
        "identity": "9" * 64,
        "images": [
            {
                "canonicalReference": f"example.invalid/{role}@{DIGEST}",
                "observedReference": f"example.invalid/{role}@{DIGEST}",
                "role": role,
            }
            for role in ("api", "postgres", "redis", "web")
        ],
        "transportPolicyIdentity": "8" * 64,
        "verifiedReleaseIdentity": "sha256:" + "6" * 64,
    }
    doctor_digest = _identity(doctor_report)
    installer_result_digest = _identity(output["installerResult"])
    doctor_execution_identity = _identity(
        {
            "canonicalAcceptanceReceiptDigests": [
                item["receiptDigest"] for item in canonical_acceptance
            ],
            "completedSteps": output["installerResult"]["completedSteps"],
            "doctorReceiptDigest": doctor_digest,
            "installerExecutionReceiptDigest": installer_result_digest,
        }
    )
    completed_commands = [
        {
            "argvDigest": _identity(
                [
                    "/usr/bin/docker",
                    "--host",
                    "unix:///var/run/docker.sock",
                    "image",
                    "inspect",
                ]
            ),
            "boundary": "RUNTIME",
            "classification": "LOCAL_DOCKER_SOCKET",
            "externalPullDisposition": "NOT_APPLICABLE",
            "operation": "docker-local-socket",
            "returnCode": 0,
        }
    ]
    egress_body = {
        "authority": "OS_ENFORCED_CANDIDATE_EGRESS_ISOLATION",
        "containerNetwork": "animemo_animemo",
        "containerNetworkInternal": True,
        "service": "animemo-updater.service",
        "serviceAddressFamilies": ["AF_UNIX", "AF_NETLINK"],
    }
    output["productionExecutionObservation"] = {
        "schema": "animemo.candidate-profile-production-execution-observation/v1",
        "doctorReport": doctor_report,
        "doctorReceiptDigest": doctor_digest,
        "doctorExecutionIdentity": doctor_execution_identity,
        "canonicalAcceptanceTests": canonical_acceptance,
        "completedSteps": ["runtime.validate", "doctor.accept"],
        "networkObservation": {
            "authority": "PRODUCTION_EXECUTION_WITH_OS_EGRESS_ISOLATION",
            "completedCommandInventoryDigest": _identity(completed_commands),
            "completedCommands": completed_commands,
            "destinationAuthority": "NONE",
            "egressIsolation": {
                **egress_body,
                "receiptDigest": _identity(egress_body),
            },
            "expectedNetworkCommandDigests": [],
            "observerIdentities": {
                "platform": runner._COMMAND_OBSERVER_IDENTITY,
                "runtime": runner._COMMAND_OBSERVER_IDENTITY,
            },
            "platformPlanDigest": "sha256:" + "7" * 64,
            "policy": "DENY_ALL",
            "retryableNetworkCommandDigests": [],
            "result": "PASS",
        },
        "externalPullObservation": {
            "authority": "PRODUCTION_EXECUTION_COMMAND_BOUNDARY",
            "inventory": [],
            "observedCount": 0,
            "observerIdentity": runner._COMMAND_OBSERVER_IDENTITY,
            "pullDeniedCommandDigests": [],
            "result": "PASS",
            "runtimeCommandInventoryDigest": _identity(completed_commands),
        },
        "imageAcquisitionReceipt": image_receipt,
        "imageAcquisitionReceiptDigest": _identity(image_receipt),
        "imageRuntimeReadbackReceipt": {
            "images": image_receipt["images"],
            "result": "PASS",
        },
    }
    output["productionExecutionObservation"][
        "imageRuntimeReadbackReceiptDigest"
    ] = _identity(
        output["productionExecutionObservation"]["imageRuntimeReadbackReceipt"]
    )
    return output


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.runtime_paths = []
        self.runtime_dependency_visible = []

    def run(self, argv, environment):
        self.calls.append((argv, environment))
        python_paths = environment["PYTHONPATH"].split(os.pathsep)
        self.runtime_paths.append(Path(python_paths[0]))
        self.runtime_dependency_visible.append(
            (Path(python_paths[0]) / "offline_dependency" / "__init__.py").is_file()
        )
        return 0, json.dumps(_installer_output()).encode(), b"secret-free"


def _write_test_wheelhouse(root: Path) -> None:
    wheelhouse = root / "installer-root" / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    with zipfile.ZipFile(
        wheelhouse / "offline_dependency-1.0-py3-none-any.whl", "w"
    ) as archive:
        archive.writestr("offline_dependency/__init__.py", b"VERIFIED = True\n")


class CandidateProfileRunnerTests(unittest.TestCase):
    def test_installer_command_is_fixed_argv_without_release_discovery(self):
        argv = runner.installer_argv(
            verified_candidate_digest=DIGEST,
            profile="FRESH_BASE",
            public_origin="https://candidate.rc14.invalid",
        )
        self.assertEqual(argv[1:6], ("-P", "-B", "-m", "installer", "candidate"))
        self.assertIn("--verified-candidate-digest", argv)
        self.assertNotIn("--source", argv)
        self.assertNotIn("--bundle-payload", argv)
        self.assertNotIn("latest", argv)
        with self.assertRaisesRegex(runner.ProfileRunnerError, "ORIGIN_INVALID"):
            runner.installer_argv(
                verified_candidate_digest=DIGEST,
                profile="FRESH_BASE",
                public_origin="https://user@example.invalid/",
            )

    def test_context_is_closed_and_rejects_policy_override(self):
        self.assertEqual(
            runner._decode_context(_encoded_context())["profile"],
            "RUNTIME_BASE_OFFLINE",
        )
        invalid = _context()
        invalid["network_policy_override"] = True
        encoded = base64.urlsafe_b64encode(canonical_json_bytes(invalid)).decode().rstrip("=")
        with self.assertRaisesRegex(runner.ProfileRunnerError, "CONTEXT_INVALID"):
            runner._decode_context(encoded)
        with self.assertRaisesRegex(runner.ProfileRunnerError, "RESULT_INVALID"):
            runner._result_json(b'{"outcome":"SUCCEEDED","outcome":"FAILED"}')

    def test_offline_execution_receipt_has_zero_network_apt_and_pull(self):
        with tempfile.TemporaryDirectory() as temporary:
            loaded = _loaded(Path(temporary))
            _write_test_wheelhouse(loaded.root)
            fake = FakeRunner()
            parsed_plan = SimpleNamespace(
                plan_digest="sha256:" + "7" * 64,
                mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                initial_capabilities=SimpleNamespace(
                    docker_cli_present=True,
                    docker_daemon_healthy=True,
                    compose_v2_present=True,
                    pg_dump_major=16,
                    psql_major=16,
                ),
                network_policy="DENY_ALL",
                actions=(),
            )
            parsed_receipt = SimpleNamespace(
                result="PASS",
                plan_digest=parsed_plan.plan_digest,
            )
            with mock.patch(
                "scripts.candidate_profile_runner.load_verified_candidate",
                return_value=loaded,
            ), mock.patch(
                "scripts.candidate_profile_runner.parse_platform_bootstrap_plan",
                return_value=parsed_plan,
            ), mock.patch(
                "scripts.candidate_profile_runner.parse_platform_bootstrap_receipt",
                return_value=parsed_receipt,
            ):
                receipt = runner.execute_profile(
                    verified_candidate_digest=loaded.verified_digest,
                    profile="RUNTIME_BASE_OFFLINE",
                    public_origin="https://candidate.rc14.invalid",
                    context_b64url=_encoded_context(),
                    runner=fake,
                )
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(
            receipt["schema"],
            "animemo.prepublication-candidate-profile-receipt-draft/v1",
        )
        self.assertNotIn("original_vm_pre_hashes", receipt)
        self.assertNotIn("original_vm_post_hashes", receipt)
        self.assertEqual(
            receipt["network_observation"]["authority"],
            "PRODUCTION_EXECUTION_WITH_OS_EGRESS_ISOLATION",
        )
        self.assertEqual(
            len(receipt["network_observation"]["completed_commands"]), 1
        )
        self.assertEqual(receipt["external_pull_observation"]["inventory"], [])
        self.assertEqual(receipt["completed_steps"][-1], "doctor.accept")
        self.assertFalse(receipt["release_authority_granted"])
        self.assertEqual(len(fake.calls), 1)
        python_paths = fake.calls[0][1]["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(len(python_paths), 2)
        self.assertEqual(
            python_paths[1], str(loaded.root / "installer-root")
        )
        self.assertEqual(fake.calls[0][1]["PYTHONSAFEPATH"], "1")
        self.assertEqual(fake.runtime_dependency_visible, [True])
        self.assertFalse(fake.runtime_paths[0].exists())

    def test_installer_success_without_production_observation_is_rejected(self):
        output = _installer_output()
        output.pop("productionExecutionObservation")
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            runner.ProfileRunnerError,
            "CANDIDATE_PROFILE_EXECUTION_OBSERVATION_INVALID",
        ):
            parsed_plan = SimpleNamespace(
                plan_digest="sha256:" + "7" * 64,
                mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                initial_capabilities=SimpleNamespace(
                    docker_cli_present=True,
                    docker_daemon_healthy=True,
                    compose_v2_present=True,
                    pg_dump_major=16,
                    psql_major=16,
                ),
                network_policy="DENY_ALL",
                actions=(),
            )
            with mock.patch(
                "scripts.candidate_profile_runner.parse_platform_bootstrap_plan",
                return_value=parsed_plan,
            ), mock.patch(
                "scripts.candidate_profile_runner.parse_platform_bootstrap_receipt",
                return_value=SimpleNamespace(
                    result="PASS", plan_digest=parsed_plan.plan_digest
                ),
            ):
                runner.build_profile_receipt(
                    loaded=_loaded(Path(temporary)),
                    profile="RUNTIME_BASE_OFFLINE",
                    context=_context(),
                    installer_output=output,
                    started_at="2026-08-25T12:00:00Z",
                    completed_at="2026-08-25T12:01:00Z",
                )

    def test_observation_negative_matrix_is_fail_closed(self):
        parsed_plan = SimpleNamespace(
            plan_digest="sha256:" + "7" * 64,
            mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
            network_policy="DENY_ALL",
            actions=(),
        )
        mutations = {
            "doctor-failed": lambda value: value["doctorReport"].update(
                overallStatus="FAIL"
            ),
            "canonical-missing": lambda value: value[
                "canonicalAcceptanceTests"
            ].pop(),
            "completed-steps-mismatch": lambda value: value[
                "completedSteps"
            ].pop(0),
            "hidden-network-command": lambda value: value[
                "networkObservation"
            ]["completedCommands"].append(
                {
                    "argvDigest": "sha256:" + "1" * 64,
                    "boundary": "PLATFORM",
                    "classification": "UNKNOWN_NETWORK_CAPABILITY",
                    "externalPullDisposition": "NOT_APPLICABLE",
                    "operation": "curl",
                    "returnCode": 0,
                }
            ),
            "external-pull": lambda value: value[
                "externalPullObservation"
            ]["inventory"].append(
                {
                    "argvDigest": "sha256:" + "2" * 64,
                    "operation": "docker-pull",
                    "referenceDigest": DIGEST,
                    "returnCode": 0,
                }
            ),
            "runtime-image-mismatch": lambda value: value[
                "imageRuntimeReadbackReceipt"
            ]["images"][0].update(observedReference="example.invalid/api@sha256:" + "0" * 64),
        }
        for name, mutate in mutations.items():
            output = _installer_output()
            observation = copy.deepcopy(output["productionExecutionObservation"])
            mutate(observation)
            if name == "hidden-network-command":
                observation["networkObservation"][
                    "completedCommandInventoryDigest"
                ] = _identity(observation["networkObservation"]["completedCommands"])
            with self.subTest(name=name), self.assertRaises(
                runner.ProfileRunnerError
            ):
                runner._production_execution_observation(
                    loaded=_loaded(Path("unused")),
                    parsed_plan=parsed_plan,
                    installer_result=output["installerResult"],
                    value=observation,
                )

    def test_online_extra_unknown_command_cannot_be_observation_bound(self):
        output = _installer_output()
        observation = output["productionExecutionObservation"]
        observation["networkObservation"]["policy"] = "APT_UBUNTU_ARCHIVE_ONLY"
        observation["networkObservation"][
            "destinationAuthority"
        ] = "UBUNTU_ARCHIVE_VERIFIED_APT_SOURCES"
        command = {
            "argvDigest": "sha256:" + "3" * 64,
            "boundary": "PLATFORM",
            "classification": "UNKNOWN_NETWORK_CAPABILITY",
            "externalPullDisposition": "NOT_APPLICABLE",
            "operation": "wget",
            "returnCode": 0,
        }
        observation["networkObservation"]["completedCommands"] = [command]
        observation["networkObservation"][
            "completedCommandInventoryDigest"
        ] = _identity([command])
        with self.assertRaisesRegex(
            runner.ProfileRunnerError,
            "CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID",
        ):
            runner._production_execution_observation(
                loaded=_loaded(Path("unused")),
                parsed_plan=SimpleNamespace(
                    plan_digest="sha256:" + "7" * 64,
                    mode=SimpleNamespace(value="ONLINE_FRESH"),
                    network_policy="APT_UBUNTU_ARCHIVE_ONLY",
                    actions=(),
                ),
                installer_result=output["installerResult"],
                value=observation,
            )

    def test_online_duplicate_successful_apt_command_is_rejected(self):
        output = _installer_output()
        observation = output["productionExecutionObservation"]
        network = observation["networkObservation"]
        network["policy"] = "APT_UBUNTU_ARCHIVE_ONLY"
        network["destinationAuthority"] = "UBUNTU_ARCHIVE_VERIFIED_APT_SOURCES"
        apt_argv = list(runner._apt_argv("update"))
        apt_digest = _identity(apt_argv)
        apt_command = {
            "argvDigest": apt_digest,
            "boundary": "PLATFORM",
            "classification": "APT_NETWORK",
            "externalPullDisposition": "NOT_APPLICABLE",
            "operation": "apt-get",
            "returnCode": 0,
        }
        commands = [
            apt_command,
            copy.deepcopy(apt_command),
            *network["completedCommands"],
        ]
        network["completedCommands"] = commands
        network["completedCommandInventoryDigest"] = _identity(commands)
        network["expectedNetworkCommandDigests"] = [apt_digest]
        network["retryableNetworkCommandDigests"] = []

        with self.assertRaisesRegex(
            runner.ProfileRunnerError,
            "CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID",
        ):
            runner._production_execution_observation(
                loaded=_loaded(Path("unused")),
                parsed_plan=SimpleNamespace(
                    plan_digest="sha256:" + "7" * 64,
                    mode=SimpleNamespace(value="ONLINE_FRESH"),
                    network_policy="APT_UBUNTU_ARCHIVE_ONLY",
                    actions=(
                        SimpleNamespace(
                            kind=SimpleNamespace(value="APT_UPDATE"),
                            packages=(),
                        ),
                    ),
                ),
                installer_result=output["installerResult"],
                value=observation,
            )

    def test_online_install_allows_one_timeout_then_one_success(self):
        output = _installer_output()
        observation = output["productionExecutionObservation"]
        network = observation["networkObservation"]
        network["policy"] = "APT_UBUNTU_ARCHIVE_ONLY"
        network["destinationAuthority"] = "UBUNTU_ARCHIVE_VERIFIED_APT_SOURCES"
        packages = ("docker.io",)
        install_digest = _identity(list(runner._apt_argv("install", packages)))
        install_command = {
            "argvDigest": install_digest,
            "boundary": "PLATFORM",
            "classification": "APT_NETWORK",
            "externalPullDisposition": "NOT_APPLICABLE",
            "operation": "apt-get",
            "returnCode": 124,
        }
        commands = [
            install_command,
            {**install_command, "returnCode": 0},
            *network["completedCommands"],
        ]
        network["completedCommands"] = commands
        network["completedCommandInventoryDigest"] = _identity(commands)
        network["expectedNetworkCommandDigests"] = [install_digest]
        network["retryableNetworkCommandDigests"] = [install_digest]

        result = runner._production_execution_observation(
            loaded=_loaded(Path("unused")),
            parsed_plan=SimpleNamespace(
                plan_digest="sha256:" + "7" * 64,
                mode=SimpleNamespace(value="ONLINE_FRESH"),
                network_policy="APT_UBUNTU_ARCHIVE_ONLY",
                actions=(
                    SimpleNamespace(
                        kind=SimpleNamespace(value="INSTALL_DOCKER"),
                        packages=packages,
                    ),
                ),
            ),
            installer_result=output["installerResult"],
            value=observation,
        )

        self.assertEqual(
            result["network_observation"]["retryable_network_command_digests"],
            [install_digest],
        )

    def test_missing_wheelhouse_stops_before_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            loaded = _loaded(Path(temporary))
            fake = FakeRunner()
            with mock.patch(
                "scripts.candidate_profile_runner.load_verified_candidate",
                return_value=loaded,
            ), self.assertRaisesRegex(
                runner.ProfileRunnerError, "CANDIDATE_PROFILE_RUNTIME_INVALID"
            ):
                runner.execute_profile(
                    verified_candidate_digest=loaded.verified_digest,
                    profile="RUNTIME_BASE_OFFLINE",
                    public_origin="https://candidate.rc14.invalid",
                    context_b64url=_encoded_context(),
                    runner=fake,
                )
        self.assertEqual(fake.calls, [])

    def test_profile_mismatch_stops_before_installer(self):
        fake = FakeRunner()
        with self.assertRaisesRegex(runner.ProfileRunnerError, "CONTEXT_MISMATCH"):
            runner.execute_profile(
                verified_candidate_digest=DIGEST,
                profile="FRESH_BASE",
                public_origin="https://candidate.rc14.invalid",
                context_b64url=_encoded_context(),
                runner=fake,
            )
        self.assertEqual(fake.calls, [])

    def test_cli_defaults_to_plan_only(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = runner.main(
                [
                    "--verified-candidate-digest",
                    DIGEST,
                    "--profile",
                    "FRESH_BASE",
                    "--public-origin",
                    "https://candidate.rc14.invalid",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "PLAN_ONLY")


if __name__ == "__main__":
    unittest.main()
