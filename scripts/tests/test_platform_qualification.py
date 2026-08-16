from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest import mock

from durability.compatibility import CompatibilityOutcome, ReasonCode
from durability.platform import (
    REQUIRED_CAPABILITIES,
    REQUIRED_REHEARSALS,
    DatabasePathEvidence,
    HostCapabilityEvidence,
    PlatformQualificationError,
    assess_platform,
    canonical_platform_qualification_bytes,
    finalize_platform_qualification,
    parse_platform_qualification,
)
from scripts import platform_qualification as producer
from scripts.platform_qualification import main

SHA = "a" * 40


def unsigned_payload() -> dict[str, object]:
    return {
        "schema": "animemo.platform-qualification/v1",
        "profile": "v1.1-standard-linux-amd64",
        "candidateSha": SHA,
        "workflow": {
            "path": ".github/workflows/platform-qualification.yml",
            "ref": "refs/heads/main",
            "sha": SHA,
        },
        "run": {"id": "12345", "attempt": 1},
        "observedAt": "2026-08-16T00:00:00Z",
        "host": {
            "os": "linux",
            "architecture": "amd64",
            "distributionId": "qualification-observed",
            "distributionVersion": "observed-not-a-floor",
            "kernel": "observed-kernel",
            "systemdVersion": "observed-systemd",
            "dockerVersion": "observed-docker",
            "composeVersion": "observed-compose",
        },
        "databasePath": {
            "dumpFormat": "plain",
            "sourceServerMajor": 16,
            "pgDumpMajor": 16,
            "psqlMajor": 16,
            "targetServerMajor": 16,
        },
        "imageDigests": {
            "postgres": "docker.io/library/postgres@sha256:" + "b" * 64,
            "redis": "docker.io/library/redis@sha256:" + "c" * 64,
        },
        "capabilities": {name: True for name in REQUIRED_CAPABILITIES},
        "rehearsals": {name: "PASS" for name in REQUIRED_REHEARSALS},
    }


class PlatformQualificationTests(unittest.TestCase):
    def test_finalize_parse_and_digest_bind_exact_observed_evidence(self) -> None:
        qualification = finalize_platform_qualification(unsigned_payload())
        restored = parse_platform_qualification(
            canonical_platform_qualification_bytes(qualification)
        )

        self.assertEqual(restored, qualification)
        self.assertRegex(restored.evidence_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(restored.database_path.psql_major, 16)
        self.assertNotIn("minimum", json.dumps(restored.as_dict()).lower())

    def test_unknown_fields_failed_capability_and_tamper_fail_closed(self) -> None:
        unknown = unsigned_payload()
        unknown["minimumDockerVersion"] = "invented"
        failed = unsigned_payload()
        failed["capabilities"]["directory_fsync"] = False  # type: ignore[index]
        for candidate in (unknown, failed):
            with (
                self.subTest(candidate=candidate),
                self.assertRaises(PlatformQualificationError),
            ):
                finalize_platform_qualification(candidate)

        qualification = finalize_platform_qualification(unsigned_payload())
        tampered = qualification.as_dict()
        tampered["host"]["dockerVersion"] = "changed"  # type: ignore[index]
        with self.assertRaisesRegex(
            PlatformQualificationError, "PLATFORM_DIGEST_MISMATCH"
        ):
            parse_platform_qualification(json.dumps(tampered).encode())

    def test_assessment_uses_capabilities_and_exact_qualified_database_path(
        self,
    ) -> None:
        qualification = finalize_platform_qualification(unsigned_payload())
        host = HostCapabilityEvidence(
            os="linux",
            architecture="amd64",
            profile="v1.1-standard-linux-amd64",
            capabilities={name: True for name in REQUIRED_CAPABILITIES},
            database_path=qualification.database_path,
        )
        compatible = assess_platform(host, qualification)
        self.assertEqual(compatible.outcome, CompatibilityOutcome.COMPATIBLE)
        self.assertEqual(compatible.reason_code, ReasonCode.PLATFORM_RUNTIME_SUPPORTED)

        capabilities = dict(host.capabilities)
        capabilities["directory_fsync"] = False
        unsupported = assess_platform(
            HostCapabilityEvidence(
                os=host.os,
                architecture=host.architecture,
                profile=host.profile,
                capabilities=capabilities,
                database_path=host.database_path,
            ),
            qualification,
        )
        self.assertEqual(unsupported.outcome, CompatibilityOutcome.UNSUPPORTED)

        unqualified_path = DatabasePathEvidence("plain", 15, 15, 16, 16)
        unsupported_path = assess_platform(
            HostCapabilityEvidence(
                os=host.os,
                architecture=host.architecture,
                profile=host.profile,
                capabilities=host.capabilities,
                database_path=unqualified_path,
            ),
            qualification,
        )
        self.assertEqual(unsupported_path.outcome, CompatibilityOutcome.UNSUPPORTED)

    def test_cli_finalizes_and_verifies_only_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            output = root / "evidence.json"
            source.write_text(json.dumps(unsigned_payload()), encoding="utf-8")

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(["finalize", "--input", str(source), "--output", str(output)]),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "verify",
                            "--input",
                            str(output),
                            "--candidate-sha",
                            SHA,
                            "--run-id",
                            "12345",
                            "--run-attempt",
                            "1",
                            "--workflow-path",
                            ".github/workflows/platform-qualification.yml",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "verify",
                            "--input",
                            str(output),
                            "--candidate-sha",
                            "d" * 40,
                        ]
                    ),
                    2,
                )
                self.assertEqual(
                    main(
                        [
                            "verify",
                            "--input",
                            str(output),
                            "--candidate-sha",
                            SHA,
                            "--run-id",
                            "54321",
                        ]
                    ),
                    2,
                )

    def test_collector_requires_real_github_identity_and_exact_rehearsal_markers(
        self,
    ) -> None:
        environment = {
            "GITHUB_ACTIONS": "true",
            "RUNNER_OS": "Linux",
            "RUNNER_ARCH": "X64",
            "GITHUB_SHA": SHA,
            "GITHUB_WORKFLOW_SHA": SHA,
            "GITHUB_WORKFLOW_REF": (
                "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
            ),
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "2",
        }

        def command(arguments) -> str:
            values = {
                ("systemd", "--version"): "systemd 255 (255.4-1ubuntu8)",
                (
                    "docker",
                    "version",
                    "--format",
                    "{{.Server.Version}}",
                ): "26.1.3",
                ("docker", "compose", "version", "--short"): "2.27.1",
            }
            return values[tuple(arguments)]

        filesystem = {
            "directory_fsync": True,
            "file_fsync": True,
            "loopback_port_binding": True,
            "nofollow_regular_file": True,
            "posix_owner_mode": True,
            "same_directory_atomic_replace": True,
            "single_link_file": True,
            "unix_socket_permissions": True,
        }
        docker = {
            "compose_profiles": True,
            "compose_v2": True,
            "compose_wait": True,
            "docker_daemon": True,
            "immutable_image_digest": True,
        }
        postgres = {
            "postgres_plain_dump": True,
            "postgres_psql_restore": True,
        }
        database_path = {
            "dumpFormat": "plain",
            "sourceServerMajor": 16,
            "pgDumpMajor": 16,
            "psqlMajor": 16,
            "targetServerMajor": 16,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markers = root / "markers"
            markers.mkdir()
            for name in REQUIRED_REHEARSALS:
                (markers / name).write_text(SHA + "\n", encoding="ascii")
            with (
                mock.patch.object(
                    producer, "_read_os_release", return_value=("ubuntu", "24.04")
                ),
                mock.patch.object(producer.platform, "machine", return_value="x86_64"),
                mock.patch.object(
                    producer.platform, "release", return_value="6.11.0-observed"
                ),
                mock.patch.object(
                    producer, "_filesystem_capabilities", return_value=filesystem
                ),
                mock.patch.object(
                    producer,
                    "_docker_capabilities",
                    return_value=(docker, 16),
                ),
                mock.patch.object(producer, "_systemd_capability", return_value=True),
                mock.patch.object(
                    producer,
                    "_postgres_capabilities",
                    return_value=(postgres, database_path),
                ),
            ):
                qualification = producer.collect_platform_qualification(
                    candidate_sha=SHA,
                    postgres_image=producer.QUALIFIED_POSTGRES_IMAGE,
                    redis_image=producer.QUALIFIED_REDIS_IMAGE,
                    source_database_url="postgresql://qualification/source",
                    target_database_url="postgresql://qualification/target",
                    rehearsal_directory=markers,
                    probe_root=root / "probe",
                    environ=environment,
                    command=command,
                    clock=lambda: datetime(2026, 8, 16, tzinfo=UTC),
                )

            self.assertEqual(qualification.candidate_sha, SHA)
            self.assertEqual(qualification.workflow["sha"], SHA)
            self.assertEqual(qualification.run, {"id": "12345", "attempt": 2})
            self.assertEqual(
                qualification.image_digests["postgres"],
                producer.QUALIFIED_POSTGRES_IMAGE,
            )
            self.assertEqual(
                set(qualification.capabilities), set(REQUIRED_CAPABILITIES)
            )
            self.assertTrue(all(qualification.capabilities.values()))
            self.assertEqual(
                qualification.rehearsals,
                {name: "PASS" for name in REQUIRED_REHEARSALS},
            )

            (markers / "doctor_complete").unlink()
            with self.assertRaisesRegex(
                producer.QualificationProbeError,
                "PLATFORM_REHEARSAL_EVIDENCE_INVALID",
            ):
                producer._rehearsal_evidence(markers, SHA)

        invalid_environment = dict(environment, GITHUB_ACTIONS="false")
        with self.assertRaisesRegex(
            producer.QualificationProbeError,
            "PLATFORM_GITHUB_HOSTED_CONTEXT_REQUIRED",
        ):
            producer._github_identity(SHA, invalid_environment)

    def test_collector_rejects_any_dependency_image_outside_release_authority(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            producer.QualificationProbeError,
            "PLATFORM_IMAGE_AUTHORITY_MISMATCH",
        ):
            producer._docker_capabilities(
                lambda _: "",
                postgres_image="docker.io/library/postgres@sha256:" + "f" * 64,
                redis_image=producer.QUALIFIED_REDIS_IMAGE,
                run_identity="12345-1",
            )


if __name__ == "__main__":
    unittest.main()
