from __future__ import annotations

import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from durability.instance import (
    LocalLocatorStore,
    load_instance_locator,
    parse_instance_locator,
    publish_instance_locator,
    release_identity_from_manifest,
    replace_instance_locator,
)
from durability.managed_config import (
    LocalManagedConfigStore,
    parse_managed_config,
    plan_config_change,
)
from scripts.tests.test_durability_instance import locator_payload
from scripts.tests.test_managed_config import (
    INSTANCE_ID,
    NEXT_REVISION,
    REVISION,
    encoded,
)
from updater.binding import CanonicalRuntimeBinding
from updater.deployment import HostPaths, ImmutableComposeDeployment
from updater.errors import StateError
from updater.runtime import (
    CanonicalInstanceRegistry,
    HostAgentRuntime,
    InitialAdoptionRequest,
)
from updater.tests.test_executor import manifest


class AdoptionReleaseSource:
    def __init__(self, verified):
        self.verified = verified
        self.requests = []

    def fetch_verified(
        self,
        version,
        *,
        updater_version,
        refresh=False,
    ):
        self.requests.append((version, refresh))
        return copy.deepcopy(self.verified)


class AdoptionDeployment:
    def verify_deployment_contract(self, target):
        return None

    def verify_health(self, target):
        return None

    def inspect_runtime_contracts(self, target):
        return {
            "databaseContract": target["compatibility"]["database"]["contract"],
            "configurationContract": target["compatibility"]["configuration"][
                "contract"
            ],
        }

    def inspect_enabled_plugin_apis(self, target):
        return {2}


class CanonicalRuntimeBindingTests(unittest.TestCase):
    def build_binding(self, root: Path):
        config_root = root / "config"
        runtime_root = root / "runtime"
        config_root.mkdir()
        runtime_root.mkdir()
        config_root.chmod(0o700)
        runtime_root.chmod(0o750)
        config_store = LocalManagedConfigStore(
            config_root=config_root,
            runtime_root=runtime_root,
        )
        config = parse_managed_config(encoded())
        config_store.write(config, expected_revision=None, must_not_exist=True)

        payload = locator_payload()
        payload["instanceId"] = INSTANCE_ID
        payload["configRevision"] = REVISION
        locator_store = LocalLocatorStore.testing(root / "instance.json")
        locator = parse_instance_locator(payload)
        published = publish_instance_locator(locator, store=locator_store)
        registry = CanonicalInstanceRegistry(store=locator_store)
        deployment = ImmutableComposeDeployment(
            HostPaths.testing(
                app=root / "app",
                data=root / "data",
                state=root / "state",
            )
        )
        binding = CanonicalRuntimeBinding(
            registry=registry,
            config_store=config_store,
            deployment=deployment,
        )
        return binding, config_store, locator_store, deployment, config, published

    def test_refresh_rejects_config_revision_that_is_not_in_the_locator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding, config_store, _, deployment, current, _ = self.build_binding(root)
            _, changed = plan_config_change(
                current,
                next_revision=NEXT_REVISION,
                public_origin="https://changed.example",
            )
            config_store.write(changed, expected_revision=REVISION)

            with self.assertRaisesRegex(
                StateError, "Managed configuration does not match"
            ):
                binding.refresh()

            self.assertEqual(deployment.paths.app_root, (root / "app").resolve())
            self.assertEqual(deployment.managed_environment, {})
            self.assertFalse(config_store.runtime_env_path.exists())

    def test_refresh_rebinds_compose_after_config_and_locator_advance_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                binding,
                config_store,
                locator_store,
                deployment,
                current,
                published,
            ) = self.build_binding(root)
            _, changed = plan_config_change(
                current,
                next_revision=NEXT_REVISION,
                public_origin="https://changed.example",
            )
            config_store.write(changed, expected_revision=REVISION)
            changed_locator = dataclasses.replace(
                published.locator,
                config_revision=NEXT_REVISION,
                public_origin="https://changed.example",
            )
            replace_instance_locator(
                changed_locator,
                expected_digest=published.digest,
                store=locator_store,
            )

            refreshed = binding.refresh()

            self.assertEqual(refreshed.locator.config_revision, NEXT_REVISION)
            self.assertEqual(deployment.paths.locator_digest, refreshed.digest)
            self.assertEqual(deployment.paths.public_origin, "https://changed.example")
            self.assertEqual(
                deployment.managed_environment["ANIMEMO_CONFIG_REVISION"],
                NEXT_REVISION,
            )
            self.assertTrue(config_store.runtime_env_path.is_file())


class InitialAdoptionRecoveryTests(unittest.TestCase):
    def test_partial_fresh_adoption_is_reconciled_by_the_updater_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = manifest("v1.0.0", "1")
            runtime = HostAgentRuntime.testing(
                app_root=root / "app",
                data_root=root / "data",
                state_root=root / "state",
                socket_path=root / "run" / "updater.sock",
                bootstrap_manifest=root / "unused-bootstrap.json",
            )
            source = AdoptionReleaseSource(target)
            deployment = AdoptionDeployment()
            runtime.agent.source = source
            runtime.deployment = deployment
            runtime.agent.executor.deployment = deployment
            payload = locator_payload()
            payload["releaseIdentity"] = dict(
                release_identity_from_manifest(target)
            )
            request = InitialAdoptionRequest(
                locator=parse_instance_locator(payload),
                manifest=copy.deepcopy(target),
            )

            with (
                mock.patch.object(
                    runtime.runtime_state,
                    "initialize_from_manifest",
                    side_effect=OSError("injected runtime-state failure"),
                ),
                self.assertRaisesRegex(StateError, "manual recovery"),
            ):
                runtime.adopt_initial_release(request)

            operation = runtime.agent.operations.list()[0]
            self.assertEqual(operation["status"], "manual_recovery_required")
            self.assertEqual(runtime.slots.read()["current"], target)

            reconciled = runtime.reconcile(
                operation["id"],
                f"RECONCILE {operation['id']}",
            )

            self.assertEqual(reconciled["status"], "reconciled")
            self.assertEqual(source.requests, [("v1.0.0", True), ("v1.0.0", True)])
            self.assertEqual(runtime.runtime_state.read()["enabledPluginApis"], [2])
            self.assertEqual(
                load_instance_locator(runtime.locator_store).release_identity,
                release_identity_from_manifest(target),
            )
            self.assertIsNone(runtime.agent.operations.recovery_block())


if __name__ == "__main__":
    unittest.main()
