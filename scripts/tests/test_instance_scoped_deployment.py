"""Public contract tests for the v2 instance-scoped deployment boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from durability.instance import (
    INSTANCE_LOCATOR_PATH,
    LOCATOR_SCHEMA_VERSION,
    STANDARD_DEPLOYMENT_PROFILE,
    InstanceLocator,
    InstanceName,
    ListenIdentity,
    LocatorError,
    instance_locator_payload,
    instance_namespace,
    parse_instance_locator,
)
from durability.ownership import (
    LocalOwnershipReceiptStore,
    create_ownership_receipt,
    ownership_receipt_payload,
    parse_ownership_receipt,
)
from installer.cli import _parser
from installer.restore_production import ProductionRestoreDestination
from installer.runtime import TargetClass, TargetEvidence
from updater.deployment import HostPaths, ImmutableComposeDeployment

UUID_A = "12345678-1234-4234-9234-123456789abc"
REVISION = "22345678-1234-4234-9234-123456789abc"
DIGEST = "sha256:" + "a" * 64


def locator(name: str = "default") -> InstanceLocator:
    namespace = instance_namespace(name)
    return InstanceLocator(
        schema_version=2,
        instance_name=InstanceName(name),
        instance_id=UUID_A,
        app_root=namespace.app_root,
        data_root=namespace.data_root,
        updater_state_root=namespace.updater_state_root,
        updater_runtime_root=namespace.updater_runtime_root,
        deployment_profile="v1.1-instance-scoped",
        compose_project=namespace.compose_project,
        updater_service=namespace.updater_service,
        updater_socket_path=namespace.updater_socket_path,
        listen=ListenIdentity("127.0.0.1", 8088),
        public_origin="https://example.test",
        managed_config_path=namespace.managed_config_path,
        config_revision=REVISION,
        release_identity={
            "version": "v1.1.0-rc.7",
            "channel": "rc",
            "commit": "b" * 40,
            "manifestDigest": DIGEST,
            "apiDigest": DIGEST,
            "webDigest": DIGEST,
        },
        ownership_receipt_digest=DIGEST,
    )


class InstanceNameContractTests(unittest.TestCase):
    def test_01_default_mapping(self):
        ns = instance_namespace("default")
        self.assertEqual(str(ns.app_root), "/opt/animemo-instances/default")
        self.assertEqual(str(ns.data_root), "/data/animemo-instances/default")

    def test_02_rc_mapping(self):
        ns = instance_namespace("v1-1-rc")
        self.assertEqual(
            str(ns.locator_path),
            "/var/lib/animemo-updater/instances/v1-1-rc/instance.json",
        )
        self.assertEqual(str(ns.backup_root), "/data/animemo-instances/v1-1-rc/backups")

    def test_03_two_instances_have_disjoint_roots(self):
        a, b = instance_namespace("default"), instance_namespace("v1-1-rc")
        self.assertTrue(set(a.owned_roots).isdisjoint(b.owned_roots))

    def test_04_compose_projects_differ(self):
        self.assertNotEqual(
            instance_namespace("default").compose_project,
            instance_namespace("v1-1-rc").compose_project,
        )

    def test_05_sockets_differ(self):
        self.assertNotEqual(
            instance_namespace("default").updater_socket_path,
            instance_namespace("v1-1-rc").updater_socket_path,
        )

    def test_06_locks_differ(self):
        self.assertNotEqual(
            instance_namespace("default").update_lock_path,
            instance_namespace("v1-1-rc").update_lock_path,
        )

    def test_07_operations_differ(self):
        self.assertNotEqual(
            instance_namespace("default").operations_root,
            instance_namespace("v1-1-rc").operations_root,
        )

    def test_08_release_slots_differ(self):
        self.assertNotEqual(
            instance_namespace("default").release_slots_root,
            instance_namespace("v1-1-rc").release_slots_root,
        )

    def test_09_managed_configs_differ(self):
        self.assertNotEqual(
            instance_namespace("default").managed_config_path,
            instance_namespace("v1-1-rc").managed_config_path,
        )

    def test_10_systemd_units_differ(self):
        self.assertEqual(instance_namespace("default").updater_service, "animemo-updater@default.service")
        self.assertEqual(instance_namespace("v1-1-rc").updater_service, "animemo-updater@v1-1-rc.service")

    def test_11_invalid_names_fail_closed(self):
        invalid = (
            "", "A", "a_b", "a.b", "a/b", "a\\b", "a b", "例", "-a",
            "a-", "a%2fb", "a;b", "a@b", "a" * 33, "api", "web", "postgres",
            "redis", "updater", "root", "system", "instances", "current",
            "previous", "releases", "bootstrap", "cache", "runtime",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(LocatorError):
                InstanceName(value)

    def test_12_path_inputs_fail(self):
        for value in ("/tmp/x", "../x", "a/../b", "C:\\tmp"):
            with self.subTest(value=value), self.assertRaises(LocatorError):
                InstanceName(value)

    def test_13_unicode_and_casefold_collisions_fail(self):
        for value in ("Default", "DEFAULT", "ｄｅｆａｕｌｔ", "ı"):
            with self.subTest(value=value), self.assertRaises(LocatorError):
                InstanceName(value)

    def test_14_namespace_has_no_root_override_parameters(self):
        with self.assertRaises(TypeError):
            instance_namespace("default", app_root="/tmp")


class LocatorV2ContractTests(unittest.TestCase):
    def test_15_v1_locator_fails(self):
        payload = instance_locator_payload(locator())
        payload["schemaVersion"] = 1
        with self.assertRaises(LocatorError):
            parse_instance_locator(payload)

    def test_16_unknown_locator_field_fails(self):
        payload = instance_locator_payload(locator())
        payload["other"] = True
        with self.assertRaises(LocatorError):
            parse_instance_locator(payload)

    def test_17_locator_path_mismatch_fails(self):
        payload = instance_locator_payload(locator())
        payload["appRoot"] = "/opt/animemo-instances/other"
        with self.assertRaises(LocatorError):
            parse_instance_locator(payload)

    def test_18_ownership_receipt_tamper_fails(self):
        payload = instance_locator_payload(locator())
        payload["ownershipReceiptDigest"] = "sha256:" + "0" * 63 + "x"
        with self.assertRaises(LocatorError):
            parse_instance_locator(payload)

    def test_19_locator_v2_identity(self):
        parsed = parse_instance_locator(instance_locator_payload(locator("v1-1-rc")))
        self.assertEqual(parsed.instance_name, "v1-1-rc")
        self.assertEqual(parsed.schema_version, 2)

    def test_20_locator_contract_constants(self):
        self.assertEqual(LOCATOR_SCHEMA_VERSION, 2)
        self.assertEqual(STANDARD_DEPLOYMENT_PROFILE, "v1.1-instance-scoped")
        self.assertEqual(
            str(INSTANCE_LOCATOR_PATH),
            "/var/lib/animemo-updater/instances/default/instance.json",
        )


class OwnershipReceiptContractTests(unittest.TestCase):
    def receipt(self, name: str = "v1-1-rc"):
        return create_ownership_receipt(
            instance_name=name,
            instance_id=UUID_A,
            listen_host="127.0.0.1",
            listen_port=18088,
            release_identity=locator(name).release_identity,
            created_at="2026-08-23T00:00:00Z",
        )

    def test_21_receipt_round_trip_is_instance_scoped(self):
        receipt = self.receipt()
        parsed = parse_ownership_receipt(
            (json.dumps(ownership_receipt_payload(receipt)) + "\n").encode()
        )
        self.assertEqual(parsed.instance_name, "v1-1-rc")
        self.assertEqual(parsed.owned_networks, ("animemo-v1-1-rc_animemo",))

    def test_22_tampered_receipt_digest_fails_closed(self):
        payload = ownership_receipt_payload(self.receipt())
        payload["listen"]["port"] = 18089
        with self.assertRaisesRegex(LocatorError, "OWNERSHIP_RECEIPT_DIGEST_INVALID"):
            parse_ownership_receipt(json.dumps(payload).encode())

    def test_23_store_rejects_receipt_for_another_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalOwnershipReceiptStore.testing(
                Path(directory), instance_name="default"
            )
            with self.assertRaisesRegex(
                LocatorError, "OWNERSHIP_RECEIPT_INSTANCE_MISMATCH"
            ):
                store.publish(self.receipt())

    def test_24_store_rejects_hard_linked_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalOwnershipReceiptStore.testing(
                root, instance_name="v1-1-rc"
            )
            store.publish(self.receipt())
            try:
                os.link(store.path, root / "receipt-alias.json")
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            with self.assertRaisesRegex(LocatorError, "PRIVATE_FILE_INVALID"):
                store.read()

    def test_25_instance_a_receipt_mutation_leaves_instance_b_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a_root, b_root = root / "a", root / "b"
            a_root.mkdir(mode=0o700)
            b_root.mkdir(mode=0o700)
            if os.name != "nt":
                self.assertEqual(a_root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(b_root.stat().st_mode & 0o777, 0o700)
            a = LocalOwnershipReceiptStore.testing(a_root, instance_name="default")
            b = LocalOwnershipReceiptStore.testing(b_root, instance_name="v1-1-rc")
            b.publish(self.receipt())
            before = b.path.read_bytes()

            a.publish(self.receipt("default"))

            self.assertEqual(b.path.read_bytes(), before)
            self.assertEqual(b.read().instance_name, "v1-1-rc")


class DeploymentConsumerContractTests(unittest.TestCase):
    def test_26_host_paths_bind_locator_namespace(self):
        item = locator("v1-1-rc")
        from durability.instance import InstanceSnapshot

        paths = HostPaths.production(InstanceSnapshot(item, DIGEST, DIGEST))
        self.assertEqual(paths.compose_project, "animemo-v1-1-rc")
        self.assertEqual(paths.state_root, Path("/var/lib/animemo-updater/instances/v1-1-rc"))

    def test_27_compose_command_has_explicit_instance_project(self):
        class Runner:
            def run(self, argv, **kwargs):
                self.argv = argv
                return type("Result", (), {"stdout": "", "stderr": ""})()

        runner = Runner()
        paths = HostPaths.testing(
            app=Path("/tmp/a"), data=Path("/tmp/b"), state=Path("/tmp/c"),
            instance_name="v1-1-rc",
        )
        deployment = ImmutableComposeDeployment(paths, runner=runner)
        deployment.validate_compose(
            {
                "release": {"version": "v1.1.0-rc.7", "commit": "a" * 40, "channel": "rc"},
                "compatibility": {
                    "database": {"contract": "db-v1"},
                    "configuration": {"contract": "config-v1"},
                },
                "images": {
                    role: {"digest": "sha256:" + character * 64}
                    for role, character in (("api", "a"), ("web", "b"), ("postgres", "c"), ("redis", "d"))
                },
            }
        )
        self.assertIn("animemo-v1-1-rc", runner.argv)

    def test_28_cli_defaults_instance(self):
        args = _parser().parse_args(["install", "--channel", "rc", "--public-origin", "https://example.test"])
        self.assertEqual(args.instance, "default")

    def test_29_cli_accepts_rc_instance(self):
        args = _parser().parse_args(["install", "--channel", "rc", "--instance", "v1-1-rc", "--public-origin", "https://example.test"])
        self.assertEqual(args.instance, "v1-1-rc")

    def test_30_cli_rejects_invalid_instance(self):
        with self.assertRaises(SystemExit):
            _parser().parse_args(["install", "--channel", "rc", "--instance", "../x", "--public-origin", "https://example.test"])

    def test_31_updater_template_contract(self):
        unit = Path("deploy/updater/animemo-updater@.service").read_text(encoding="utf-8")
        self.assertIn("--instance %i", unit)
        self.assertIn("RuntimeDirectory=animemo-updater/%i", unit)
        self.assertIn("StateDirectory=animemo-updater/instances/%i", unit)

    def test_32_release_schema_profile_v2(self):
        schema = json.loads(Path("release/release-manifest.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["deployment"]["properties"]["profile"]["const"], "v1.1-instance-scoped")

    def test_33_deployment_contract_v2_documented(self):
        text = Path("docs/instance-scoped-deployment-contract-v2.md").read_text(encoding="utf-8")
        self.assertIn("schemaVersion = 2", text)
        self.assertIn("UNSUPPORTED_NOT_AUTO_ADOPTED", text)

    def test_34_compose_declares_instance_labels_and_mounts(self):
        base = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
        runtime = Path("updater/docker-compose.runtime.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("io.animemo.instance-name", base)
        self.assertNotIn("${ANIMEMO_DATA_ROOT:?", base)
        self.assertIn("io.animemo.instance-name", runtime)
        self.assertIn("${ANIMEMO_DATA_ROOT:?", runtime)

    def test_35_restore_destination_uses_requested_target_namespace(self):
        target = TargetEvidence(TargetClass.ABSENT, DIGEST)
        destination = ProductionRestoreDestination(
            target, instance_namespace("v1-1-rc")
        ).snapshot
        self.assertEqual(destination.instance_name, "v1-1-rc")
        self.assertIn("/opt/animemo-instances/v1-1-rc", destination.canonical_roots)
        self.assertNotIn("/opt/animemo-instances/default", destination.canonical_roots)

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI unavailable")
    def test_36_real_docker_compose_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trusted_env = root / "trusted.env"
            common = (
                "ANIMEMO_CONFIG_REVISION=22345678-1234-4234-9234-123456789abc\n"
                "ANIMEMO_LISTEN_HOST=127.0.0.1\nANIMEMO_LISTEN_PORT=18088\n"
                "ANIMEMO_TEST_DATA_ROOT=/tmp/animemo-instance-test\n"
                "ANIMEMO_PUBLIC_ORIGIN=https://example.test\n"
                "POSTGRES_DB=animemo\nPOSTGRES_USER=animemo\nPOSTGRES_PASSWORD=test-only\n"
                "ANIMEMO_API_IMAGE=example.invalid/api@sha256:" + "a" * 64 + "\n"
                "ANIMEMO_WEB_IMAGE=example.invalid/web@sha256:" + "b" * 64 + "\n"
                "ANIMEMO_POSTGRES_IMAGE=postgres@sha256:" + "c" * 64 + "\n"
                "ANIMEMO_REDIS_IMAGE=redis@sha256:" + "d" * 64 + "\n"
            )
            trusted_env.write_text(common, encoding="utf-8")
            child_env = {
                **os.environ,
                "ANIMEMO_TEST_MANAGED_ENV_PATH": str(trusted_env),
            }
            trusted = subprocess.run(
                [
                    "docker", "compose", "--env-file", str(trusted_env),
                    "-f", "deploy/docker-compose.yml",
                    "-f", "deploy/docker-compose.build.yml", "config",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                check=False,
            )
            self.assertEqual(trusted.returncode, 0, trusted.stderr)
            self.assertNotIn("io.animemo.instance-name", trusted.stdout)

            env = root / "managed.env"
            env.write_text(
                common
                + "ANIMEMO_DATA_ROOT=/tmp/animemo-instance-test\n"
                + "ANIMEMO_INSTANCE_NAME=v1-1-rc\n"
                + "ANIMEMO_INSTANCE_ID=12345678-1234-4234-9234-123456789abc\n"
                + "ANIMEMO_COMPOSE_PROJECT=animemo-v1-1-rc\n"
                + "ANIMEMO_MANAGED_ENV_PATH=" + str(env).replace("\\", "/") + "\n"
                + "ANIMEMO_UPDATER_RUNTIME_ROOT=/tmp/animemo-updater-v1-1-rc\n"
                + "ANIMEMO_RELEASE_VERSION=v1.1.0-rc.7\n"
                + "ANIMEMO_RELEASE_COMMIT=" + "e" * 40 + "\n"
                + "ANIMEMO_RELEASE_CHANNEL=rc\n"
                + "ANIMEMO_DATABASE_CONTRACT=animemo-db-v1\n"
                + "ANIMEMO_CONFIGURATION_CONTRACT=animemo-config-v1\n",
                encoding="utf-8",
            )
            for compose_files in (
                ("deploy/docker-compose.yml", "updater/docker-compose.runtime.yml"),
                ("updater/docker-compose.runtime.yml", "deploy/docker-compose.yml"),
            ):
                with self.subTest(compose_files=compose_files):
                    result = subprocess.run(
                        [
                            "docker", "compose", "--project-name", "animemo-v1-1-rc",
                            "--env-file", str(env),
                            "-f", compose_files[0], "-f", compose_files[1], "config",
                        ],
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("io.animemo.instance-name: v1-1-rc", result.stdout)
                    self.assertIn("/tmp/animemo-instance-test/postgres", result.stdout)


if __name__ == "__main__":
    unittest.main()
