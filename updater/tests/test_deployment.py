from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from release.contract import build_manifest, deployment_contract_digest
from scripts.tests.trust_kit_fixture import contract_only_test_pretrust_bytes
from updater.deployment import (
    CANDIDATE_NETWORK_OVERRIDE_TEXT,
    INITIAL_HTTP_READY_ATTEMPTS,
    INITIAL_HTTP_READY_INTERVAL_SECONDS,
    HostPaths,
    ImmutableComposeDeployment,
)
from updater.errors import StateError

TEST_COMPOSE_BYTES = b"services: {}\n"
UPDATER_OVERLAY = Path(__file__).parents[1] / "docker-compose.runtime.yml"
DEPLOYMENT_FILES = [
    {
        "path": "deploy/docker-compose.yml",
        "sha256": "sha256:" + hashlib.sha256(TEST_COMPOSE_BYTES).hexdigest(),
    },
    {
        "path": "updater/docker-compose.runtime.yml",
        "sha256": "sha256:" + hashlib.sha256(UPDATER_OVERLAY.read_bytes()).hexdigest(),
    },
]
MATERIALS_DIGEST = "sha256:" + "c" * 64
DEPLOYMENT_DIGEST = deployment_contract_digest(
    {
        "schemaVersion": 2,
        "profile": "v1.1-instance-scoped",
        "platform": "linux/amd64",
        "archive": {
            "name": "installer-materials.tar",
            "sha256": MATERIALS_DIGEST,
            "size": 1,
            "format": "tar",
        },
        "files": DEPLOYMENT_FILES,
        "materials": sorted([
            {
                "path": "updater/__init__.py",
                "sha256": "sha256:" + "d" * 64,
                "size": 1,
                "mode": "0644",
            }
        ] + [
            {
                "path": path,
                "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
                "size": len(value),
                "mode": "0755" if path.endswith("/offline-release-verifier") else "0644",
            }
            for path, value in contract_only_test_pretrust_bytes().items()
        ], key=lambda item: item["path"]),
    }
)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.service_health = {
            name: "healthy 0" for name in ["postgres", "redis", "api", "web"]
        }
        self.container_logs = {name: "" for name in ["postgres", "redis", "api", "web"]}
        self.container_errors = {
            name: "" for name in ["postgres", "redis", "api", "web"]
        }
        self.container_images = {}
        self.container_labels = {}
        self.web_release_identity = {}
        self.web_proxy_ip = "172.30.0.5"
        self.migration_plans = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        stdout = ""
        if "ps" in argv and "--services" in argv:
            stdout = "postgres\nredis\napi\nweb\n"
        elif "ps" in argv and "-q" in argv:
            stdout = f"{argv[-1]}-container\n"
        elif argv[:2] == ["/usr/bin/docker", "inspect"]:
            service = str(argv[-1]).removesuffix("-container")
            template = argv[3]
            if ".State.Health" in template:
                stdout = self.service_health[service] + "\n"
            elif ".NetworkSettings.Networks" in template:
                stdout = json.dumps(
                    {
                        "animemo-default_animemo": {
                            "IPAddress": self.web_proxy_ip,
                        }
                    }
                ) + "\n"
            elif ".Config.Image" in template:
                stdout = self.container_images[service] + "\n"
            else:
                for label, value in self.container_labels[service].items():
                    if label in template:
                        stdout = value + "\n"
                        break
        elif argv[:2] == ["/usr/bin/docker", "exec"]:
            stdout = (
                "\n".join(
                    self.web_release_identity[key]
                    for key in ("version", "commit", "channel")
                )
                + "\n"
            )
        elif argv[:2] == ["/usr/bin/docker", "logs"]:
            service = str(argv[-1]).removesuffix("-container")
            stdout = self.container_logs[service]
        elif "list_enabled_plugin_apis" in " ".join(argv):
            stdout = "[2]\n"
        elif "showmigrations --plan" in " ".join(argv):
            stdout = self.migration_plans.pop(0)
        stderr = ""
        if argv[:2] == ["/usr/bin/docker", "logs"]:
            service = str(argv[-1]).removesuffix("-container")
            stderr = self.container_errors[service]
        return type("Result", (), {"stdout": stdout, "stderr": stderr})()

    def write_gzip(self, argv, path, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        path.write_bytes(b"placeholder")
        return {"sha256": "a" * 64, "uncompressedBytes": 128}


def manifest():
    return build_manifest(
        version="v1.0.0",
        channel="stable",
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + "a" * 64,
        web_digest="sha256:" + "b" * 64,
        deployment_contract_sha256=DEPLOYMENT_DIGEST,
        deployment_files=DEPLOYMENT_FILES,
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        installer_materials_sha256=MATERIALS_DIGEST,
        promoted_from="v1.0.0-rc.1",
    )


class ImmutableComposeDeploymentTests(unittest.TestCase):
    def make(self, directory, *, http_statuses=None, release_identity=None):
        root = Path(directory)
        app = root / "app"
        data = root / "data"
        state = root / "state"
        memory_info = root / "meminfo"
        (app / "deploy").mkdir(parents=True)
        (app / "deploy" / "docker-compose.yml").write_bytes(TEST_COMPOSE_BYTES)
        (data / "config").mkdir(parents=True)
        (data / "config" / "animemo.json").write_text("{}\n", encoding="utf-8")
        state.mkdir()
        (state / "managed.env").write_text(
            "POSTGRES_USER=animemo\nPOSTGRES_DB=animemo\nPOSTGRES_PASSWORD=test-placeholder\n",
            encoding="utf-8",
        )
        for name in ["backups", "plugins", "logs", "media"]:
            (data / name).mkdir(parents=True, exist_ok=True)
        memory_info.write_text("MemAvailable:    2097152 kB\n", encoding="utf-8")
        runner = FakeRunner()
        target = manifest()
        runner.container_images = {
            "api": "ghcr.io/yanyuhanyue/animemo-api@"
            + target["images"]["api"]["digest"],
            "web": "ghcr.io/yanyuhanyue/animemo-web@"
            + target["images"]["web"]["digest"],
        }
        artifact_version = target["release"]["promotedFrom"]
        artifact_identity = {
            "org.opencontainers.image.version": artifact_version,
            "org.opencontainers.image.revision": target["release"]["commit"],
            "cc.animemo.release.channel": "rc",
        }
        runner.container_labels = {
            service: {
                "io.animemo.instance-name": "default",
                "io.animemo.instance-id": "00000000-0000-4000-8000-000000000000",
                "io.animemo.compose-project": "animemo-default",
                **(artifact_identity if service in {"api", "web"} else {}),
            }
            for service in ["postgres", "redis", "api", "web"]
        }
        effective_identity = release_identity or {
            "version": target["release"]["version"],
            "commit": target["release"]["commit"],
            "channel": target["release"]["channel"],
        }
        runner.web_release_identity = dict(effective_identity)
        statuses = http_statuses or {}
        probes = []

        def http_probe(path, *, host, port, forwarded_proto):
            probes.append((path, host, port, forwarded_proto))
            return statuses.get(path, 200)

        def release_probe(*, host, port, forwarded_proto):
            probes.append(("release-identity", host, port, forwarded_proto))
            return {
                "status": "ok",
                "release": dict(effective_identity),
                "contracts": {
                    "database": target["compatibility"]["database"]["contract"],
                    "configuration": target["compatibility"]["configuration"][
                        "contract"
                    ],
                },
            }

        deployment = ImmutableComposeDeployment(
            HostPaths.testing(app=app, data=data, state=state),
            managed_environment={
                "POSTGRES_USER": "animemo",
                "POSTGRES_DB": "animemo",
                "POSTGRES_PASSWORD": "test-placeholder",
                "ANIMEMO_DATA_ROOT": str(data),
                "TRUSTED_PROXY_IPS": f"{runner.web_proxy_ip}/32",
            },
            runner=runner,
            stable_observations=1,
            memory_info_path=memory_info,
            http_probe=http_probe,
            release_probe=release_probe,
        )
        return deployment, runner, probes

    def test_container_ownership_label_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            runner.container_labels["api"]["io.animemo.instance-id"] = (
                "ffffffff-ffff-4fff-8fff-ffffffffffff"
            )
            with self.assertRaisesRegex(StateError, "ownership label is invalid"):
                deployment._container_id(manifest(), "api")

    def test_exact_web_proxy_returns_the_running_owned_container_ipv4(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)

            trusted_proxy = deployment.exact_web_proxy(manifest())

            self.assertEqual(trusted_proxy, "172.30.0.5/32")
            compose_calls = [call[0] for call in runner.calls if "compose" in call[0]]
            self.assertTrue(
                any(call[-4:] == ("ps", "--all", "-q", "web") for call in compose_calls)
            )

    def test_reconcile_api_keeps_the_web_proxy_container_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)

            deployment.reconcile_api(manifest())

            command = runner.calls[-1][0]
            self.assertEqual(
                command[-10:],
                (
                    "up",
                    "--pull",
                    "never",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "api",
                ),
            )
            self.assertNotIn("web", command)

    def test_all_candidate_compose_up_and_run_paths_forbid_implicit_pull(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            current = manifest()

            deployment.start_datastores(current)
            deployment.start_application(current)
            deployment.reconcile_application(current)
            deployment.reconcile_api(current)
            deployment.switch(current)
            deployment.migrate(current)
            deployment.bootstrap(current)

            pull_sensitive_commands = [
                call[0]
                for call in runner.calls
                if "compose" in call[0]
                and any(command in call[0] for command in ("run", "up"))
            ]
            self.assertEqual(len(pull_sensitive_commands), 7)
            for command in pull_sensitive_commands:
                subcommand = "up" if "up" in command else "run"
                index = command.index(subcommand)
                self.assertEqual(
                    command[index + 1 : index + 3], ("--pull", "never")
                )

    def test_candidate_compose_uses_only_the_exact_internal_network_override(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            private = deployment.paths.data_root / "private"
            private.mkdir(mode=0o700)
            override = private / "candidate-network-isolation.yml"
            override.write_text(CANDIDATE_NETWORK_OVERRIDE_TEXT, encoding="utf-8")
            deployment.candidate_network_override = override

            deployment.start_datastores(manifest())

            command = runner.calls[-1][0]
            override_index = command.index(str(override))
            self.assertEqual(command[override_index - 1], "-f")
            self.assertLess(override_index, command.index("up"))

            override.write_text("networks: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "override is unsafe"):
                deployment.start_datastores(manifest())

    def test_candidate_compose_rejects_a_missing_network_override(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            deployment.candidate_network_override = (
                deployment.paths.data_root
                / "private"
                / "candidate-network-isolation.yml"
            )

            with self.assertRaisesRegex(StateError, "override is unavailable"):
                deployment.start_datastores(manifest())

    def test_exact_web_proxy_rejects_multiple_networks(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            original_run = runner.run

            def run(argv, **kwargs):
                if argv[:2] == ["/usr/bin/docker", "inspect"] and (
                    ".NetworkSettings.Networks" in argv[3]
                ):
                    return type(
                        "Result",
                        (),
                        {
                            "stdout": json.dumps(
                                {
                                    "owned": {"IPAddress": "172.30.0.5"},
                                    "foreign": {"IPAddress": "172.31.0.5"},
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                return original_run(argv, **kwargs)

            runner.run = run
            with self.assertRaisesRegex(StateError, "network identity is invalid"):
                deployment.exact_web_proxy(manifest())

    def test_pre_update_backup_uses_the_instance_compose_project(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)

            deployment.backup_database("a" * 32)

            argv = runner.calls[-1][0]
            self.assertEqual(argv[argv.index("--project-name") + 1], "animemo-default")

    def test_compose_uses_only_managed_configuration_and_a_minimal_child_environment(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            with mock.patch.dict(
                os.environ,
                {
                    "POSTGRES_PASSWORD": "ambient-must-not-win",
                    "COMPOSE_FILE": "attacker.yml",
                    "UNRELATED_SECRET": "must-not-propagate",
                },
                clear=False,
            ):
                deployment._compose(manifest(), "config", "--quiet")

            argv, kwargs = runner.calls[-1]
            self.assertNotIn(".env.production", " ".join(argv))
            self.assertIn(str(deployment.paths.managed_env_path), argv)
            self.assertEqual(kwargs["env"]["POSTGRES_PASSWORD"], "test-placeholder")
            self.assertNotIn("COMPOSE_FILE", kwargs["env"])
            self.assertNotIn("UNRELATED_SECRET", kwargs["env"])

    def write_backup(
        self, deployment, *, created_at=None, content=b"SELECT 1;\n", compressed=None
    ):
        backup_root = deployment.paths.data_root / "backups"
        backup = backup_root / "verified.sql.gz"
        if compressed is None:
            with gzip.open(backup, "wb") as handle:
                handle.write(content)
        else:
            backup.write_bytes(compressed)
        metadata = {
            "path": str(backup),
            "createdAt": (created_at or datetime.now(timezone.utc))
            .isoformat()
            .replace("+00:00", "Z"),
            "compressedSha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        }
        (backup_root / "verified.sql.gz.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )

    def test_pull_and_switch_use_only_exact_digest_api_and_web(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            target = manifest()

            deployment.pull(target)
            deployment.switch(target)

            commands = [call[0] for call in runner.calls]
            self.assertIn(
                (
                    "/usr/bin/docker",
                    "pull",
                    "ghcr.io/yanyuhanyue/animemo-api@"
                    + target["images"]["api"]["digest"],
                ),
                commands,
            )
            self.assertIn(
                (
                    "/usr/bin/docker",
                    "pull",
                    "ghcr.io/yanyuhanyue/animemo-web@"
                    + target["images"]["web"]["digest"],
                ),
                commands,
            )
            switch = commands[-1]
            self.assertEqual(
                switch[-11:],
                (
                    "up",
                    "--pull",
                    "never",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "api",
                    "web",
                ),
            )
            self.assertNotIn("postgres", switch)
            self.assertNotIn("redis", switch)

    def test_switch_atomically_replaces_a_linked_runtime_env_without_mutating_the_source(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            target = manifest()
            outside = Path(directory) / "outside.env"
            outside.write_text("DO_NOT_CHANGE=1\n", encoding="utf-8")
            deployment.paths.state_root.mkdir(parents=True, exist_ok=True)
            try:
                deployment.runtime_env.symlink_to(outside)
            except OSError:
                deployment.runtime_env.hardlink_to(outside)

            deployment.switch(target)

            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE=1\n")
            self.assertFalse(deployment.runtime_env.is_symlink())
            self.assertEqual(
                deployment.runtime_env.read_text(encoding="utf-8"),
                "ANIMEMO_API_IMAGE=ghcr.io/yanyuhanyue/animemo-api@"
                + target["images"]["api"]["digest"]
                + "\nANIMEMO_WEB_IMAGE=ghcr.io/yanyuhanyue/animemo-web@"
                + target["images"]["web"]["digest"]
                + "\nANIMEMO_POSTGRES_IMAGE=docker.io/library/postgres@"
                + target["images"]["postgres"]["digest"]
                + "\nANIMEMO_REDIS_IMAGE=docker.io/library/redis@"
                + target["images"]["redis"]["digest"]
                + "\nANIMEMO_RELEASE_VERSION=v1.0.0"
                + "\nANIMEMO_RELEASE_COMMIT="
                + "1" * 40
                + "\nANIMEMO_RELEASE_CHANNEL=stable"
                + "\nANIMEMO_DATABASE_CONTRACT=animemo-db-v1"
                + "\nANIMEMO_CONFIGURATION_CONTRACT=animemo-config-v1\n",
            )

    def test_switch_uses_the_authoritative_live_contracts_not_the_image_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            target = manifest()

            deployment.switch(
                target,
                live_contracts={
                    "databaseContract": "animemo-db-v2",
                    "configurationContract": "animemo-config-v2",
                },
            )

            runtime = deployment.runtime_env.read_text(encoding="utf-8")
            self.assertIn("ANIMEMO_DATABASE_CONTRACT=animemo-db-v2\n", runtime)
            self.assertIn("ANIMEMO_CONFIGURATION_CONTRACT=animemo-config-v2\n", runtime)
            switch_environment = runner.calls[-1][1]["env"]
            self.assertEqual(
                switch_environment["ANIMEMO_DATABASE_CONTRACT"], "animemo-db-v2"
            )
            self.assertEqual(
                switch_environment["ANIMEMO_CONFIGURATION_CONTRACT"],
                "animemo-config-v2",
            )

    def test_switch_does_not_follow_a_precreated_atomic_temporary_link(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            outside = Path(directory) / "outside.tmp"
            outside.write_text("DO_NOT_CHANGE=1\n", encoding="utf-8")
            deployment.paths.state_root.mkdir(parents=True, exist_ok=True)
            predictable = deployment.runtime_env.with_name(
                f".{deployment.runtime_env.name}.{os.getpid()}.tmp"
            )
            predictable.hardlink_to(outside)

            deployment.switch(manifest())

            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE=1\n")

    def test_migration_and_bootstrap_use_target_api_image_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            target = manifest()

            deployment.migrate(target)
            deployment.bootstrap(target)

            commands = [call[0] for call in runner.calls]
            self.assertEqual(
                commands[0][-6:],
                ("run", "--pull", "never", "--rm", "--no-deps", "migration"),
            )
            self.assertEqual(
                commands[1][-6:],
                ("run", "--pull", "never", "--rm", "--no-deps", "bootstrap"),
            )
            for _, kwargs in runner.calls:
                self.assertEqual(
                    kwargs["env"]["ANIMEMO_API_IMAGE"],
                    "ghcr.io/yanyuhanyue/animemo-api@"
                    + target["images"]["api"]["digest"],
                )

    def test_runtime_contract_inspection_uses_the_running_api_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            target = manifest()

            contracts = deployment.inspect_runtime_contracts(target)

            commands = [call[0] for call in runner.calls]
            self.assertEqual(
                commands[0][-8:],
                (
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "manage.py",
                    "migrate",
                    "--check",
                    "--noinput",
                ),
            )
            self.assertEqual(
                commands[1][-7:],
                ("exec", "-T", "api", "python", "manage.py", "check", "--deploy"),
            )
            self.assertEqual(
                contracts,
                {
                    "databaseContract": "animemo-db-v1",
                    "configurationContract": "animemo-config-v1",
                },
            )

    def test_database_transition_inspection_distinguishes_current_target_and_partial(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            current = manifest()
            target = manifest()

            scenarios = [
                (
                    "current",
                    "[X]  journal.0001_initial\n",
                    "[X]  journal.0001_initial\n[ ]  journal.0002_additive\n",
                ),
                (
                    "target",
                    "[X]  journal.0001_initial\n",
                    "[X]  journal.0001_initial\n[X]  journal.0002_additive\n",
                ),
                (
                    "indeterminate",
                    "[X]  journal.0001_initial\n",
                    "[X]  journal.0001_initial\n[X]  journal.0002_a\n[ ]  journal.0003_b\n",
                ),
            ]
            for expected, current_plan, target_plan in scenarios:
                with self.subTest(expected=expected):
                    runner.migration_plans = [current_plan, target_plan]
                    self.assertEqual(
                        deployment.inspect_database_transition(current, target),
                        expected,
                    )

    def test_custom_or_symlink_escape_paths_are_rejected(self):
        with self.assertRaises(TypeError):
            HostPaths.production(None)

    def test_recent_backup_requires_fresh_metadata_and_valid_gzip_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            self.write_backup(deployment)
            deployment.verify_recent_backup()

            self.write_backup(
                deployment, created_at=datetime.now(timezone.utc) - timedelta(hours=25)
            )
            with self.assertRaisesRegex(Exception, "older than"):
                deployment.verify_recent_backup()

            self.write_backup(deployment, compressed=b"not-gzip")
            with self.assertRaisesRegex(Exception, "gzip"):
                deployment.verify_recent_backup()

    def test_recent_backup_rejects_linked_metadata_and_backup_files(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            backup_root = deployment.paths.data_root / "backups"
            outside_backup = Path(directory) / "outside.sql.gz"
            with gzip.open(outside_backup, "wb") as handle:
                handle.write(b"SELECT 1;\n")
            linked_backup = backup_root / "linked.sql.gz"
            linked_backup.hardlink_to(outside_backup)
            metadata_payload = {
                "path": str(linked_backup),
                "createdAt": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "compressedSha256": hashlib.sha256(
                    outside_backup.read_bytes()
                ).hexdigest(),
            }
            metadata = backup_root / "linked.sql.gz.json"
            metadata.write_text(json.dumps(metadata_payload), encoding="utf-8")

            with self.assertRaisesRegex(Exception, "link"):
                deployment.verify_recent_backup()

            linked_backup.unlink()
            metadata.unlink()
            outside_metadata = Path(directory) / "outside.json"
            outside_metadata.write_text(json.dumps(metadata_payload), encoding="utf-8")
            metadata.hardlink_to(outside_metadata)

            with self.assertRaisesRegex(Exception, "link"):
                deployment.verify_recent_backup()

    def test_recent_backup_reports_invalid_metadata_as_state_error(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            metadata = deployment.paths.data_root / "backups" / "latest.json"
            metadata.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(StateError, "metadata is invalid"):
                deployment.verify_recent_backup()

    def test_recent_backup_rejects_metadata_without_a_backup_path(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            metadata = deployment.paths.data_root / "backups" / "latest.json"
            metadata.write_text(
                json.dumps(
                    {
                        "createdAt": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "compressedSha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StateError, "metadata is invalid"):
                deployment.verify_recent_backup()

    def test_preflight_blocks_when_available_memory_is_below_the_host_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            deployment.memory_info_path.write_text(
                "MemAvailable:    262144 kB\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(Exception, "memory"):
                deployment.preflight(manifest())

    def test_preflight_and_reconciliation_reject_deployment_file_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            deployment.compose_file.write_text(
                "services:\n  attacker: {}\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(StateError, "differs from the Manifest"):
                deployment.preflight(manifest())
            with self.assertRaisesRegex(StateError, "differs from the Manifest"):
                deployment.verify_health(manifest())

    def test_preflight_requires_all_current_services_to_be_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            runner.service_health["redis"] = "unhealthy 0"

            with self.assertRaisesRegex(Exception, "redis"):
                deployment.preflight(manifest())

    def test_preflight_requires_current_api_and_web_http_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, probes = self.make(
                directory, http_statuses={"/health/": 503}
            )

            with self.assertRaisesRegex(Exception, "/health/"):
                deployment.preflight(manifest())
            self.assertIn(("/health/", "ci.example.test", 8088, "https"), probes)

    def test_preflight_requires_backup_availability_before_release_work(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)

            with self.assertRaisesRegex(Exception, "backup"):
                deployment.preflight(manifest())

            self.write_backup(deployment)
            deployment.preflight(manifest())

    def test_stable_window_checks_all_public_contract_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, probes = self.make(directory)

            deployment.verify_health(manifest())

            self.assertEqual(
                [probe[0] for probe in probes],
                [
                    "/health/",
                    "/",
                    "/login",
                    "/api/schema/",
                    "/api/docs/",
                    "release-identity",
                ],
            )

    def test_stable_health_waits_for_transient_host_http_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, probes = self.make(directory)
            health_attempts = 0

            def transient_http_probe(path, *, host, port, forwarded_proto):
                nonlocal health_attempts
                probes.append((path, host, port, forwarded_proto))
                if path == "/health/":
                    health_attempts += 1
                    if health_attempts == 1:
                        raise ConnectionResetError("host port is not ready")
                return 200

            deployment.http_probe = transient_http_probe
            with mock.patch("updater.deployment.time.sleep") as sleep:
                deployment.verify_health(manifest())

            self.assertEqual(health_attempts, 2)
            sleep.assert_called_once_with(INITIAL_HTTP_READY_INTERVAL_SECONDS)

    def test_stable_health_waits_for_transient_incomplete_http_response(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            health_attempts = 0

            def transient_http_probe(path, *, host, port, forwarded_proto):
                nonlocal health_attempts
                if path == "/health/":
                    health_attempts += 1
                    if health_attempts == 1:
                        raise http.client.RemoteDisconnected("response closed")
                return 200

            deployment.http_probe = transient_http_probe
            with mock.patch("updater.deployment.time.sleep") as sleep:
                deployment.verify_health(manifest())

            self.assertEqual(health_attempts, 2)
            sleep.assert_called_once_with(INITIAL_HTTP_READY_INTERVAL_SECONDS)

    def test_initial_http_non_200_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            health_attempts = 0

            def non_200_then_success(path, *, host, port, forwarded_proto):
                nonlocal health_attempts
                if path == "/health/":
                    health_attempts += 1
                    return 503 if health_attempts == 1 else 200
                return 200

            deployment.http_probe = non_200_then_success
            with (
                mock.patch("updater.deployment.time.sleep") as sleep,
                self.assertRaisesRegex(StateError, "HTTP 503"),
            ):
                deployment.verify_health(manifest())

            self.assertEqual(health_attempts, 1)
            sleep.assert_not_called()

    def test_initial_http_publication_wait_remains_bounded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            health_attempts = 0

            def unavailable_http_probe(path, *, host, port, forwarded_proto):
                nonlocal health_attempts
                if path == "/health/":
                    health_attempts += 1
                raise ConnectionRefusedError("host port is unavailable")

            deployment.http_probe = unavailable_http_probe
            with (
                mock.patch("updater.deployment.time.sleep") as sleep,
                self.assertRaisesRegex(StateError, "/health/"),
            ):
                deployment.verify_health(manifest())

            self.assertEqual(health_attempts, INITIAL_HTTP_READY_ATTEMPTS)
            self.assertEqual(sleep.call_count, INITIAL_HTTP_READY_ATTEMPTS - 1)
            self.assertTrue(
                all(
                    call == mock.call(INITIAL_HTTP_READY_INTERVAL_SECONDS)
                    for call in sleep.call_args_list
                )
            )

    def test_stable_health_binds_exact_images_rc_artifact_and_effective_stable_identity(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)

            deployment.verify_health(manifest())

            commands = [call[0] for call in runner.calls]
            self.assertTrue(
                any(
                    any(".Config.Image" in part for part in command)
                    for command in commands
                )
            )
            self.assertTrue(
                any(
                    any("org.opencontainers.image.version" in part for part in command)
                    for command in commands
                )
            )
            self.assertTrue(
                any(command[:2] == ("/usr/bin/docker", "exec") for command in commands)
            )

    def test_stable_health_rejects_a_wrong_running_image_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            runner.container_images["web"] = (
                "ghcr.io/yanyuhanyue/animemo-web@sha256:" + "0" * 64
            )

            with self.assertRaisesRegex(StateError, "web image identity"):
                deployment.verify_health(manifest())

    def test_stable_health_rejects_an_api_effective_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(
                directory,
                release_identity={
                    "version": "v1.0.0-rc.1",
                    "commit": "1" * 40,
                    "channel": "rc",
                },
            )

            with self.assertRaisesRegex(StateError, "effective release identity"):
                deployment.verify_health(manifest())

    def test_stable_window_rejects_http_5xx_and_critical_logs(self):
        for log_line in [
            '127.0.0.1 - - "GET / HTTP/1.1" 502 173',
            "Traceback (most recent call last):",
        ]:
            with (
                self.subTest(log_line=log_line),
                tempfile.TemporaryDirectory() as directory,
            ):
                deployment, runner, _ = self.make(directory)
                runner.container_logs["web"] = log_line

                with self.assertRaisesRegex(Exception, "logs"):
                    deployment.verify_health(manifest())

    def test_stable_window_scans_container_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            runner.container_errors["api"] = "Traceback (most recent call last):"

            with self.assertRaisesRegex(Exception, "logs"):
                deployment.verify_health(manifest())


if __name__ == "__main__":
    unittest.main()
