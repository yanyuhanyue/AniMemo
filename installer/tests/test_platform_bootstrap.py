from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

from durability.platform import REQUIRED_CAPABILITIES
from installer.platform_bootstrap import (
    PLATFORM_BOOTSTRAP_ERROR_CODES,
    PLATFORM_PACKAGE_POLICY,
    BootstrapHostFacts,
    PlatformBootstrapActionKind,
    PlatformBootstrapError,
    PlatformBootstrapMode,
    PlatformCommandResult,
    ProductionPlatformBootstrap,
    SubprocessPlatformCommandRunner,
    _apt_argv,
    _apt_sources_evidence,
    _compose_plugin_identity,
    _verify_existing_docker_transaction,
    parse_platform_bootstrap_plan,
    parse_platform_bootstrap_receipt,
    validate_platform_bootstrap_receipt,
)
from installer.runtime import InstallTransportSource

_ORIGINAL_PATH_LSTAT = Path.lstat


def _root_owned_fixture_lstat(path: Path) -> os.stat_result:
    values = list(_ORIGINAL_PATH_LSTAT(path))
    values[4] = 0
    return os.stat_result(values)


def fresh_base_facts() -> BootstrapHostFacts:
    return BootstrapHostFacts(
        distribution_id="ubuntu",
        distribution_major="24.04",
        architecture="amd64",
        effective_uid=0,
        apt_available=True,
        apt_sources_trusted=True,
        apt_sources_identity="sha256:" + "a" * 64,
        systemd_available=True,
        docker_cli_present=False,
        docker_cli_available=False,
        docker_cli_trusted=True,
        docker_cli_identity=None,
        docker_service_active=False,
        docker_daemon_healthy=False,
        docker_daemon_identity=None,
        docker_socket_present=False,
        docker_socket_local=False,
        docker_socket_identity=None,
        compose_v2_present=False,
        compose_v2_available=False,
        compose_v2_identity=None,
        docker_config_identity="ABSENT",
        pg_dump_major=None,
        psql_major=None,
        installed_policy_packages=(),
    )


def qualified_existing_facts(
    *,
    compose: bool = True,
    postgres_major: int | None = 16,
    installed: tuple[str, ...] = (),
) -> BootstrapHostFacts:
    return BootstrapHostFacts(
        distribution_id="ubuntu",
        distribution_major="24.04",
        architecture="amd64",
        effective_uid=0,
        apt_available=True,
        apt_sources_trusted=True,
        apt_sources_identity="sha256:" + "a" * 64,
        systemd_available=True,
        docker_cli_present=True,
        docker_cli_available=True,
        docker_cli_trusted=True,
        docker_cli_identity="sha256:" + "b" * 64,
        docker_service_active=True,
        docker_daemon_healthy=True,
        docker_daemon_identity="sha256:" + "c" * 64,
        docker_socket_present=True,
        docker_socket_local=True,
        docker_socket_identity="sha256:" + "d" * 64,
        compose_v2_present=compose,
        compose_v2_available=compose,
        compose_v2_identity="sha256:" + "e" * 64 if compose else None,
        docker_config_identity="ABSENT",
        pg_dump_major=postgres_major,
        psql_major=postgres_major,
        installed_policy_packages=installed,
    )


class SequenceFacts:
    def __init__(self, *facts: BootstrapHostFacts) -> None:
        self.facts = list(facts)
        self.calls = 0

    def __call__(self) -> BootstrapHostFacts:
        index = min(self.calls, len(self.facts) - 1)
        self.calls += 1
        return self.facts[index]


class RunnerFixture:
    def __init__(self, *, fail_token: str | None = None, apt_lock: bool = False):
        self.fail_token = fail_token
        self.apt_lock = apt_lock
        self.calls: list[tuple[tuple[str, ...], int, dict[str, str]]] = []

    def run(self, argv, *, timeout, environment):
        argv = tuple(argv)
        self.calls.append((argv, timeout, dict(environment)))
        joined = " ".join(argv)
        if (
            self.fail_token is not None
            and self.fail_token in joined
            and not (argv[0] == "/usr/bin/apt-cache" and argv[-2] == "policy")
        ):
            stderr = b"Could not get lock" if self.apt_lock else b"closed failure"
            return PlatformCommandResult(1, stderr=stderr)
        if argv[0] == "/usr/bin/apt-cache" and argv[-2] == "policy":
            package = argv[-1]
            return PlatformCommandResult(
                0,
                stdout=(
                    f"{package}:\n"
                    "  Installed: (none)\n"
                    "  Candidate: 1.0-1ubuntu1\n"
                    "  Version table:\n"
                    "     1.0-1ubuntu1 500\n"
                    "        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages\n"
                ).encode(),
            )
        return PlatformCommandResult(0)


class PlatformBootstrapFreshPlanTests(unittest.TestCase):
    def test_compose_plugin_shadow_path_is_rejected(self) -> None:
        shadow = "/usr/local/libexec/docker/cli-plugins/docker-compose"

        with (
            mock.patch.object(
                Path,
                "exists",
                autospec=True,
                side_effect=lambda path: path.as_posix() == shadow,
            ),
            mock.patch.object(Path, "is_symlink", autospec=True, return_value=False),
            self.assertRaises(PlatformBootstrapError) as raised,
        ):
            _compose_plugin_identity()

        self.assertEqual(
            raised.exception.code,
            "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
        )

    def test_rc13_fresh_base_derives_closed_plan_before_strict_installer_plan(
        self,
    ) -> None:
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=fresh_base_facts,
            clock=lambda: "2026-08-25T04:30:00Z",
        )

        plan = bootstrap.plan(
            transport_source=InstallTransportSource.OFFICIAL_MIRROR,
        )

        self.assertEqual(plan.mode, PlatformBootstrapMode.ONLINE_FRESH)
        self.assertEqual(
            tuple(action.kind for action in plan.actions),
            (
                PlatformBootstrapActionKind.APT_UPDATE,
                PlatformBootstrapActionKind.INSTALL_DOCKER,
                PlatformBootstrapActionKind.INSTALL_COMPOSE,
                PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT,
                PlatformBootstrapActionKind.ENABLE_DOCKER_DAEMON,
            ),
        )
        self.assertEqual(
            tuple(package for action in plan.actions for package in action.packages),
            ("docker.io", "docker-compose-v2", "postgresql-client-16"),
        )

    def test_mode_and_package_list_are_not_caller_inputs(self) -> None:
        parameters = inspect.signature(ProductionPlatformBootstrap.plan).parameters
        self.assertEqual(set(parameters), {"self", "transport_source"})
        self.assertNotIn("mode", parameters)
        self.assertNotIn("packages", parameters)
        with mock.patch.dict(
            os.environ,
            {
                "ANIMEMO_PLATFORM_MODE": "OFFLINE_VALIDATE_ONLY",
                "ANIMEMO_PLATFORM_PACKAGES": "arbitrary-package",
            },
        ):
            plan = ProductionPlatformBootstrap(
                facts_collector=fresh_base_facts,
                clock=lambda: "2026-08-25T04:30:00Z",
            ).plan(transport_source=InstallTransportSource.GITHUB)
        self.assertEqual(plan.mode, PlatformBootstrapMode.ONLINE_FRESH)
        self.assertEqual(
            [package for action in plan.actions for package in action.packages],
            ["docker.io", "docker-compose-v2", "postgresql-client-16"],
        )

    def test_existing_docker_preserves_runtime_and_only_adds_missing_capabilities(
        self,
    ) -> None:
        plan = ProductionPlatformBootstrap(
            facts_collector=lambda: qualified_existing_facts(
                compose=False,
                postgres_major=None,
            ),
            clock=lambda: "2026-08-25T04:30:00Z",
        ).plan(transport_source=InstallTransportSource.OFFICIAL_MIRROR)

        self.assertEqual(plan.mode, PlatformBootstrapMode.ONLINE_EXISTING_DOCKER)
        self.assertEqual(
            tuple(action.kind for action in plan.actions),
            (
                PlatformBootstrapActionKind.APT_UPDATE,
                PlatformBootstrapActionKind.INSTALL_COMPOSE,
                PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT,
            ),
        )
        self.assertNotIn(
            PlatformBootstrapActionKind.INSTALL_DOCKER,
            tuple(action.kind for action in plan.actions),
        )
        self.assertNotIn(
            PlatformBootstrapActionKind.ENABLE_DOCKER_DAEMON,
            tuple(action.kind for action in plan.actions),
        )
        self.assertEqual(plan.docker_daemon_policy, "PRESERVE_NO_RESTART")

    def test_local_bundle_is_validate_only_and_requires_every_capability(self) -> None:
        plan = ProductionPlatformBootstrap(
            facts_collector=qualified_existing_facts,
            clock=lambda: "2026-08-25T04:30:00Z",
        ).plan(transport_source=InstallTransportSource.LOCAL_BUNDLE)
        self.assertEqual(plan.mode, PlatformBootstrapMode.OFFLINE_VALIDATE_ONLY)
        self.assertEqual(
            tuple(action.kind for action in plan.actions),
            (PlatformBootstrapActionKind.VALIDATE_ONLY,),
        )
        self.assertEqual(plan.network_policy, "DENY_ALL")

        with self.assertRaises(PlatformBootstrapError) as raised:
            ProductionPlatformBootstrap(
                facts_collector=lambda: qualified_existing_facts(compose=False),
                clock=lambda: "2026-08-25T04:30:00Z",
            ).plan(transport_source=InstallTransportSource.LOCAL_BUNDLE)
        self.assertEqual(
            raised.exception.code,
            "PLATFORM_BOOTSTRAP_OFFLINE_CAPABILITY_MISSING",
        )

    def test_unsupported_and_inconsistent_host_facts_fail_closed(self) -> None:
        fixtures = (
            (
                replace(fresh_base_facts(), distribution_id="debian"),
                "PLATFORM_BOOTSTRAP_OS_UNSUPPORTED",
            ),
            (
                replace(fresh_base_facts(), architecture="arm64"),
                "PLATFORM_BOOTSTRAP_ARCH_UNSUPPORTED",
            ),
            (
                replace(fresh_base_facts(), effective_uid=1000),
                "PLATFORM_BOOTSTRAP_ROOT_REQUIRED",
            ),
            (
                replace(fresh_base_facts(), apt_available=False),
                "PLATFORM_BOOTSTRAP_PACKAGE_MANAGER_UNAVAILABLE",
            ),
            (
                replace(fresh_base_facts(), compose_v2_available=True),
                "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
            ),
            (
                replace(
                    fresh_base_facts(),
                    docker_cli_present=True,
                    docker_cli_identity="sha256:" + "b" * 64,
                ),
                "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
            ),
            (
                replace(
                    fresh_base_facts(),
                    compose_v2_present=True,
                ),
                "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
            ),
            (
                replace(
                    fresh_base_facts(),
                    docker_cli_available=True,
                    docker_daemon_healthy=False,
                ),
                "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
            ),
            (
                replace(
                    fresh_base_facts(),
                    apt_sources_trusted=False,
                    apt_sources_identity=None,
                ),
                "PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID",
            ),
            (
                replace(fresh_base_facts(), docker_cli_trusted=False),
                "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
            ),
            (
                replace(fresh_base_facts(), pg_dump_major=16, psql_major=15),
                "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
            ),
        )
        for facts, code in fixtures:
            with (
                self.subTest(code=code),
                self.assertRaises(PlatformBootstrapError) as raised,
            ):
                ProductionPlatformBootstrap(
                    facts_collector=lambda facts=facts: facts,
                    clock=lambda: "2026-08-25T04:30:00Z",
                ).plan(transport_source=InstallTransportSource.GITHUB)
            self.assertEqual(raised.exception.code, code)

    def test_plan_is_canonical_closed_and_time_is_not_part_of_identity(self) -> None:
        first = ProductionPlatformBootstrap(
            facts_collector=fresh_base_facts,
            clock=lambda: "2026-08-25T04:30:00Z",
        ).plan(transport_source=InstallTransportSource.GITHUB)
        second = ProductionPlatformBootstrap(
            facts_collector=fresh_base_facts,
            clock=lambda: "2026-08-25T04:31:00Z",
        ).plan(transport_source=InstallTransportSource.GITHUB)

        self.assertEqual(first.plan_digest, second.plan_digest)
        self.assertNotEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(parse_platform_bootstrap_plan(first.canonical_bytes()), first)
        self.assertEqual(
            first.package_policy_identity, PLATFORM_PACKAGE_POLICY.identity
        )

    def test_plan_parser_rejects_duplicates_unknown_fields_and_digest_changes(
        self,
    ) -> None:
        plan = ProductionPlatformBootstrap(
            facts_collector=fresh_base_facts,
            clock=lambda: "2026-08-25T04:30:00Z",
        ).plan(transport_source=InstallTransportSource.GITHUB)
        body = plan.as_dict()
        unknown = {**body, "arbitrary": True}
        tampered = {**body, "planDigest": "sha256:" + "0" * 64}
        duplicate = plan.canonical_bytes().replace(
            b'"schemaVersion":',
            b'"schemaVersion":"duplicate","schemaVersion":',
            1,
        )
        for raw in (
            json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
            + b"\n",
            duplicate,
        ):
            with self.subTest(raw=raw[:40]), self.assertRaises(PlatformBootstrapError):
                parse_platform_bootstrap_plan(raw)


class PlatformBootstrapExecutionTests(unittest.TestCase):
    def fresh_execution(self, *, runner=None, final=None):
        initial = fresh_base_facts()
        final = final or qualified_existing_facts(
            installed=("docker.io", "docker-compose-v2", "postgresql-client-16")
        )
        facts = SequenceFacts(initial, initial, final)
        runner = runner or RunnerFixture()
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=facts,
            runner=runner,
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=lambda: nullcontext(),
        )
        plan = bootstrap.plan(transport_source=InstallTransportSource.GITHUB)
        return bootstrap, plan, runner

    def test_fresh_execution_uses_only_closed_argv_and_returns_bound_receipt(
        self,
    ) -> None:
        bootstrap, plan, runner = self.fresh_execution()

        receipt = bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)

        commands = [" ".join(call[0]) for call in runner.calls]
        self.assertTrue(any(command.endswith(" update") for command in commands))
        self.assertTrue(any(command.endswith(" docker.io") for command in commands))
        self.assertTrue(
            any(command.endswith(" docker-compose-v2") for command in commands)
        )
        self.assertTrue(
            any(command.endswith(" postgresql-client-16") for command in commands)
        )
        self.assertIn("/usr/bin/systemctl enable --now docker", commands)
        combined = "\n".join(commands).lower()
        for forbidden in (
            " upgrade",
            " autoremove",
            " full-upgrade",
            " dist-upgrade",
            "curl",
            "bash -c",
            "docker prune",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertTrue(all(call[1] <= 900 for call in runner.calls))
        self.assertTrue(
            all(
                call[2]
                == {
                    "DEBIAN_FRONTEND": "noninteractive",
                    "HOME": "/nonexistent",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                }
                for call in runner.calls
            )
        )
        self.assertEqual(receipt.plan_digest, plan.plan_digest)
        self.assertEqual(receipt.docker_daemon_before, "ABSENT")
        self.assertEqual(receipt.docker_daemon_after, "HEALTHY")
        self.assertEqual(receipt.docker_daemon_restart_count, 0)
        self.assertEqual(
            parse_platform_bootstrap_receipt(receipt.canonical_bytes(), plan=plan),
            receipt,
        )
        rendered = receipt.canonical_bytes().lower()
        for secret_word in (
            b"authorization",
            b"cookie",
            b"proxy",
            b"token",
            b"password",
        ):
            self.assertNotIn(secret_word, rendered)

    def test_exact_plan_acceptance_and_host_fact_revalidation_are_mandatory(
        self,
    ) -> None:
        bootstrap, plan, _ = self.fresh_execution()
        with self.assertRaises(PlatformBootstrapError) as raised:
            bootstrap.execute(plan, accepted_plan_digest="sha256:" + "f" * 64)
        self.assertEqual(raised.exception.code, "PLATFORM_BOOTSTRAP_PLAN_NOT_ACCEPTED")

        changed = replace(fresh_base_facts(), installed_policy_packages=("docker.io",))
        facts = SequenceFacts(fresh_base_facts(), changed)
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=facts,
            runner=RunnerFixture(),
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=lambda: nullcontext(),
        )
        plan = bootstrap.plan(transport_source=InstallTransportSource.GITHUB)
        with self.assertRaises(PlatformBootstrapError) as raised:
            bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(raised.exception.code, "PLATFORM_BOOTSTRAP_PLAN_CHANGED")

    def test_apt_and_component_failures_have_stable_specific_codes(self) -> None:
        failures = (
            (" update", False, "PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED"),
            (" update", True, "PLATFORM_BOOTSTRAP_APT_LOCK_TIMEOUT"),
            (" docker.io", False, "PLATFORM_BOOTSTRAP_DOCKER_INSTALL_FAILED"),
            (
                " docker-compose-v2",
                False,
                "PLATFORM_BOOTSTRAP_COMPOSE_INSTALL_FAILED",
            ),
            (
                " postgresql-client-16",
                False,
                "PLATFORM_BOOTSTRAP_POSTGRES_CLIENT_INSTALL_FAILED",
            ),
            (
                "systemctl enable --now docker",
                False,
                "PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED",
            ),
        )
        for token, apt_lock, expected in failures:
            runner = RunnerFixture(fail_token=token, apt_lock=apt_lock)
            bootstrap, plan, _ = self.fresh_execution(runner=runner)
            with (
                self.subTest(expected=expected),
                self.assertRaises(PlatformBootstrapError) as raised,
            ):
                bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
            self.assertEqual(raised.exception.code, expected)

    def test_untrusted_or_missing_apt_candidate_is_rejected(self) -> None:
        class UntrustedRunner(RunnerFixture):
            def run(self, argv, *, timeout, environment):
                result = super().run(argv, timeout=timeout, environment=environment)
                if argv[0] == "/usr/bin/apt-cache" and argv[-2] == "policy":
                    return PlatformCommandResult(
                        0,
                        stdout=b"Candidate: 1.0\n500 https://packages.example.invalid stable/main\n",
                    )
                return result

        bootstrap, plan, _ = self.fresh_execution(runner=UntrustedRunner())
        with self.assertRaises(PlatformBootstrapError) as raised:
            bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(
            raised.exception.code, "PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE"
        )

        class SubstitutedCandidateRunner(RunnerFixture):
            def run(self, argv, *, timeout, environment):
                result = super().run(argv, timeout=timeout, environment=environment)
                if argv[0] == "/usr/bin/apt-cache" and argv[-2] == "policy":
                    return PlatformCommandResult(
                        0,
                        stdout=(
                            b"Candidate: 9.9-evil\nVersion table:\n"
                            b"  9.9-evil 900\n"
                            b"    900 https://packages.example.invalid stable/main\n"
                            b"  1.0-1ubuntu1 500\n"
                            b"    500 http://archive.ubuntu.com/ubuntu noble/main\n"
                        ),
                    )
                return result

        bootstrap, plan, _ = self.fresh_execution(runner=SubstitutedCandidateRunner())
        with self.assertRaises(PlatformBootstrapError) as raised:
            bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(
            raised.exception.code, "PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE"
        )

    def test_post_provision_capability_failure_stops_before_any_instance_interface(
        self,
    ) -> None:
        incomplete = replace(
            qualified_existing_facts(
                installed=("docker.io", "docker-compose-v2", "postgresql-client-16")
            ),
            compose_v2_available=False,
        )
        bootstrap, plan, _ = self.fresh_execution(final=incomplete)
        with self.assertRaises(PlatformBootstrapError) as raised:
            bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(
            raised.exception.code,
            "PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED",
        )

    def test_existing_docker_never_installs_or_restarts_docker_and_is_idempotent(
        self,
    ) -> None:
        initial = qualified_existing_facts(postgres_major=None)
        final = qualified_existing_facts(
            installed=("postgresql-client-16",),
        )
        facts = SequenceFacts(initial, initial, final, final)
        runner = RunnerFixture()
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=facts,
            runner=runner,
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=lambda: nullcontext(),
        )
        plan = bootstrap.plan(transport_source=InstallTransportSource.OFFICIAL_MIRROR)
        receipt = bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        commands = [" ".join(call[0]) for call in runner.calls]
        self.assertFalse(any(command.endswith(" docker.io") for command in commands))
        self.assertFalse(any("systemctl" in command for command in commands))
        self.assertEqual(receipt.docker_daemon_before, "HEALTHY")
        self.assertEqual(receipt.docker_daemon_after, "HEALTHY")
        self.assertEqual(receipt.docker_daemon_restart_count, 0)

        second = ProductionPlatformBootstrap(
            facts_collector=lambda: final,
            clock=lambda: "2026-08-25T04:31:00Z",
        ).plan(transport_source=InstallTransportSource.OFFICIAL_MIRROR)
        self.assertEqual(
            tuple(action.kind for action in second.actions),
            (PlatformBootstrapActionKind.VALIDATE_ONLY,),
        )

        for drifted in (
            replace(final, docker_daemon_identity="sha256:" + "f" * 64),
            replace(final, docker_socket_identity="sha256:" + "f" * 64),
            replace(final, compose_v2_identity="sha256:" + "f" * 64),
        ):
            drifted_bootstrap = ProductionPlatformBootstrap(
                facts_collector=SequenceFacts(initial, initial, drifted),
                runner=RunnerFixture(),
                clock=lambda: "2026-08-25T04:32:00Z",
                lock_factory=lambda: nullcontext(),
            )
            drifted_plan = drifted_bootstrap.plan(
                transport_source=InstallTransportSource.OFFICIAL_MIRROR
            )
            with (
                self.subTest(drifted=drifted),
                self.assertRaises(PlatformBootstrapError) as raised,
            ):
                drifted_bootstrap.execute(
                    drifted_plan,
                    accepted_plan_digest=drifted_plan.plan_digest,
                )
            self.assertEqual(
                raised.exception.code,
                "PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED",
            )

    def test_existing_docker_may_install_initially_missing_compose(self) -> None:
        initial = qualified_existing_facts(compose=False)
        final = qualified_existing_facts(
            compose=True,
            installed=("docker-compose-v2",),
        )
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=SequenceFacts(initial, initial, final),
            runner=RunnerFixture(),
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=lambda: nullcontext(),
        )
        plan = bootstrap.plan(transport_source=InstallTransportSource.OFFICIAL_MIRROR)

        receipt = bootstrap.execute(
            plan,
            accepted_plan_digest=plan.plan_digest,
        )

        self.assertEqual(
            tuple(action.kind for action in plan.actions),
            (
                PlatformBootstrapActionKind.APT_UPDATE,
                PlatformBootstrapActionKind.INSTALL_COMPOSE,
            ),
        )
        self.assertEqual(receipt.installed_packages, ("docker-compose-v2",))
        self.assertEqual(receipt.docker_daemon_restart_count, 0)
        self.assertEqual(
            receipt.final_capabilities.docker_daemon_identity,
            initial.docker_daemon_identity,
        )
        self.assertRegex(
            receipt.final_capabilities.compose_v2_identity or "",
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_offline_execution_runs_no_command(self) -> None:
        facts = qualified_existing_facts()
        sequence = SequenceFacts(facts, facts, facts)
        runner = RunnerFixture(fail_token="/")
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=sequence,
            runner=runner,
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=lambda: nullcontext(),
        )
        plan = bootstrap.plan(transport_source=InstallTransportSource.LOCAL_BUNDLE)
        receipt = bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(runner.calls, [])
        self.assertEqual(receipt.installed_packages, ())
        self.assertEqual(receipt.docker_daemon_restart_count, 0)

    def test_receipt_parser_rejects_unknown_fields_and_plan_substitution(self) -> None:
        bootstrap, plan, _ = self.fresh_execution()
        receipt = bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        unknown = {**receipt.as_dict(), "secret": "value"}
        wrong_plan = {**receipt.as_dict(), "planDigest": "sha256:" + "a" * 64}
        for value in (unknown, wrong_plan):
            raw = (
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            with self.assertRaises(PlatformBootstrapError) as raised:
                parse_platform_bootstrap_receipt(raw, plan=plan)
            self.assertEqual(
                raised.exception.code, "PLATFORM_BOOTSTRAP_RECEIPT_INVALID"
            )

        substituted = replace(
            receipt,
            docker_daemon_before="ARBITRARY",
            docker_daemon_after="ARBITRARY",
            receipt_digest="",
        )
        substituted = replace(
            substituted,
            receipt_digest="sha256:"
            + hashlib.sha256(
                (
                    json.dumps(
                        substituted.identity_body(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            ).hexdigest(),
        )
        with self.assertRaises(PlatformBootstrapError) as raised:
            validate_platform_bootstrap_receipt(substituted, plan=plan)
        self.assertEqual(
            raised.exception.code,
            "PLATFORM_BOOTSTRAP_RECEIPT_INVALID",
        )

    def test_lock_conflict_is_stable_and_bounded_by_the_lock_interface(self) -> None:
        @contextmanager
        def conflict():
            raise PlatformBootstrapError("PLATFORM_BOOTSTRAP_ALREADY_RUNNING")
            yield

        initial = fresh_base_facts()
        bootstrap = ProductionPlatformBootstrap(
            facts_collector=lambda: initial,
            runner=RunnerFixture(),
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=conflict,
        )
        plan = bootstrap.plan(transport_source=InstallTransportSource.GITHUB)
        with self.assertRaises(PlatformBootstrapError) as raised:
            bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(raised.exception.code, "PLATFORM_BOOTSTRAP_ALREADY_RUNNING")


class PlatformBootstrapInvariantTests(unittest.TestCase):
    def test_apt_source_set_is_closed_and_has_a_stable_identity(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=_root_owned_fixture_lstat,
            ),
        ):
            root = Path(directory)
            sources = root / "sources.list.d"
            sources.mkdir()
            keyring = root / "ubuntu-archive-keyring.gpg"
            keyring.write_bytes(b"trusted Ubuntu archive keyring fixture")
            source = sources / "ubuntu.sources"
            source.write_text(
                "Types: deb\n"
                "URIs: http://archive.ubuntu.com/ubuntu\n"
                "Suites: noble noble-updates noble-security\n"
                "Components: main universe\n"
                "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n",
                encoding="utf-8",
            )
            trusted, identity = _apt_sources_evidence(
                list_path=root / "sources.list",
                directory=sources,
                keyring_path=keyring,
            )
            self.assertTrue(trusted)
            self.assertRegex(identity or "", r"^sha256:[0-9a-f]{64}$")

            source.write_text(
                "Types: deb\nURIs: https://packages.example.invalid/stable\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _apt_sources_evidence(
                    list_path=root / "sources.list",
                    directory=sources,
                    keyring_path=keyring,
                ),
                (False, None),
            )

    def test_apt_sources_reject_signature_bypass_and_commands_bind_fixed_paths(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=_root_owned_fixture_lstat,
            ),
        ):
            root = Path(directory)
            sources = root / "sources.list.d"
            sources.mkdir()
            keyring = root / "ubuntu-archive-keyring.gpg"
            keyring.write_bytes(b"trusted Ubuntu archive keyring fixture")
            legacy = root / "sources.list"
            legacy.write_text(
                "deb [trusted=yes] http://archive.ubuntu.com/ubuntu noble main\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _apt_sources_evidence(
                    list_path=legacy,
                    directory=sources,
                    keyring_path=keyring,
                ),
                (False, None),
            )

            legacy.unlink()
            (sources / "ubuntu.sources").write_text(
                "Types: deb\n"
                "URIs: http://archive.ubuntu.com/ubuntu\n"
                "Suites: noble\n"
                "Components: main\n"
                "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
                "Trusted: yes\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _apt_sources_evidence(
                    list_path=legacy,
                    directory=sources,
                    keyring_path=keyring,
                ),
                (False, None),
            )

        argv = _apt_argv("update")
        self.assertIn("Dir::Etc::sourcelist=/etc/apt/sources.list", argv)
        self.assertIn("Dir::Etc::sourceparts=/etc/apt/sources.list.d", argv)
        self.assertIn("Acquire::AllowInsecureRepositories=false", argv)
        self.assertIn("Acquire::AllowDowngradeToInsecureRepositories=false", argv)

    def test_existing_docker_simulation_rejects_arch_configure_and_remove_forms(
        self,
    ) -> None:
        class SimulationRunner:
            def __init__(self, output: bytes) -> None:
                self.output = output

            def run(self, argv, *, timeout, environment):
                del argv, timeout, environment
                return PlatformCommandResult(0, stdout=self.output)

        for output in (
            b"Inst docker.io:amd64 (26.1.3 Ubuntu:24.04/noble [amd64])\n",
            b"Conf docker.io (26.1.3 Ubuntu:24.04/noble [amd64])\n",
            b"Remv runc [1.1.12-0ubuntu3]\n",
        ):
            with (
                self.subTest(output=output),
                self.assertRaises(PlatformBootstrapError) as raised,
            ):
                _verify_existing_docker_transaction(
                    SimulationRunner(output),
                    ("postgresql-client-16",),
                )
            self.assertEqual(
                raised.exception.code,
                "PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID",
            )

    def test_command_timeout_terminates_a_dedicated_process_group(self) -> None:
        source = inspect.getsource(SubprocessPlatformCommandRunner.run)
        self.assertIn('start_new_session=os.name == "posix"', source)
        self.assertIn("os.killpg(process.pid, signal.SIGTERM)", source)
        self.assertIn("os.killpg(process.pid, signal.SIGKILL)", source)
        self.assertIn("process.communicate(timeout=5)", source)

    @unittest.skipUnless(os.name == "posix", "production timeout is POSIX-only")
    def test_timeout_kills_descendant_that_ignores_term_and_closes_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "descendant.pid"
            child = (
                "import os,signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"open({str(pid_file)!r},'w').write(str(os.getpid()));"
                "os.close(1);os.close(2);time.sleep(60)"
            )
            leader = (
                "import subprocess,time;"
                f"subprocess.Popen([{sys.executable!r},'-c',{child!r}]);"
                "time.sleep(60)"
            )
            result = SubprocessPlatformCommandRunner().run(
                (sys.executable, "-c", leader),
                timeout=1,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            self.assertEqual(result.returncode, 124)
            descendant = int(pid_file.read_text(encoding="ascii"))
            state_path = Path(f"/proc/{descendant}/stat")
            for _ in range(100):
                if not state_path.exists() or state_path.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            self.assertTrue(
                not state_path.exists() or state_path.read_text().split()[2] == "Z"
            )

    def test_strict_platform_capability_authority_is_unchanged(self) -> None:
        self.assertEqual(
            REQUIRED_CAPABILITIES,
            (
                "compose_profiles",
                "compose_v2",
                "compose_wait",
                "directory_fsync",
                "docker_daemon",
                "file_fsync",
                "immutable_image_digest",
                "loopback_port_binding",
                "nofollow_regular_file",
                "posix_owner_mode",
                "postgres_plain_dump",
                "postgres_psql_restore",
                "same_directory_atomic_replace",
                "single_link_file",
                "systemd_unit_lifecycle",
                "unix_socket_permissions",
            ),
        )

    def test_error_taxonomy_contains_every_required_stable_code(self) -> None:
        self.assertEqual(len(PLATFORM_BOOTSTRAP_ERROR_CODES), 19)
        self.assertIn(
            "PLATFORM_BOOTSTRAP_ALREADY_RUNNING", PLATFORM_BOOTSTRAP_ERROR_CODES
        )
        self.assertIn(
            "PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED",
            PLATFORM_BOOTSTRAP_ERROR_CODES,
        )


if __name__ == "__main__":
    unittest.main()
