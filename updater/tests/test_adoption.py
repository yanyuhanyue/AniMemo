from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from durability.instance import (
    LocatorError,
    load_instance_locator,
    parse_instance_locator,
    release_identity_from_manifest,
)
from scripts.tests.test_durability_instance import locator_payload
from updater.errors import StateError
from updater.runtime import HostAgentRuntime, InitialAdoptionRequest
from updater.tests.test_deployment import manifest


class ReleaseSourceAdapter:
    def __init__(self, verified: dict[str, object]) -> None:
        self.verified = verified
        self.requests: list[tuple[str, bool]] = []

    def fetch_verified(
        self, version: str, *, updater_version: str, refresh: bool = False
    ):
        self.requests.append((version, refresh))
        return copy.deepcopy(self.verified)


class RunningDeploymentAdapter:
    def __init__(self, paths, target: dict[str, object]) -> None:
        self.paths = paths
        self.target = target

    def verify_deployment_contract(self, target):
        if target != self.target:
            raise StateError("unexpected target")

    def verify_health(self, target):
        if target != self.target:
            raise StateError("unexpected target")

    def inspect_runtime_contracts(self, target):
        return {
            "databaseContract": target["compatibility"]["database"]["contract"],
            "configurationContract": target["compatibility"]["configuration"][
                "contract"
            ],
        }

    def inspect_enabled_plugin_apis(self, target):
        return {2}


def adoption_request(target: dict[str, object]) -> InitialAdoptionRequest:
    payload = locator_payload()
    payload["releaseIdentity"] = dict(release_identity_from_manifest(target))
    return InitialAdoptionRequest(
        locator=parse_instance_locator(payload),
        manifest=copy.deepcopy(target),
    )


class InitialAdoptionTests(unittest.TestCase):
    def make_runtime(self, root: Path, target: dict[str, object]):
        runtime = HostAgentRuntime.testing(
            app_root=root / "app",
            data_root=root / "data",
            state_root=root / "state",
            socket_path=root / "run" / "updater.sock",
            bootstrap_manifest=root / "unused-bootstrap.json",
        )
        source = ReleaseSourceAdapter(target)
        deployment = RunningDeploymentAdapter(runtime.paths, target)
        runtime.agent.source = source
        runtime.deployment = deployment
        runtime.agent.executor.deployment = deployment
        return runtime, source

    def test_exact_initial_adoption_publishes_current_and_locator_once(self):
        with tempfile.TemporaryDirectory() as directory:
            target = manifest()
            runtime, source = self.make_runtime(Path(directory), target)

            receipt = runtime.adopt_initial_release(adoption_request(target))

            slots = runtime.slots.read()
            self.assertEqual(source.requests, [("v1.0.0", True)])
            self.assertEqual(slots["current"], target)
            self.assertIsNone(slots["previous"])
            self.assertEqual(len(slots["history"]), 1)
            self.assertEqual(
                load_instance_locator(runtime.locator_store).release_identity,
                release_identity_from_manifest(target),
            )
            self.assertEqual(
                runtime.agent.operations.get(receipt.operation_id)["status"],
                "succeeded",
            )
            with self.assertRaisesRegex(StateError, "one-time"):
                runtime.adopt_initial_release(adoption_request(target))

    def test_failure_after_current_publication_enters_manual_recovery_without_locator(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            target = manifest()
            runtime, _ = self.make_runtime(Path(directory), target)

            with (
                mock.patch.object(
                    runtime.runtime_state,
                    "initialize_from_manifest",
                    side_effect=OSError("injected persistent-state failure"),
                ),
                self.assertRaisesRegex(StateError, "manual recovery"),
            ):
                runtime.adopt_initial_release(adoption_request(target))

            self.assertEqual(runtime.slots.read()["current"], target)
            self.assertIsNone(runtime.slots.read()["previous"])
            operation = runtime.agent.operations.list()[0]
            self.assertEqual(operation["status"], "manual_recovery_required")
            with self.assertRaises(LocatorError) as missing:
                load_instance_locator(runtime.locator_store)
            self.assertEqual(missing.exception.code, "LOCATOR_MISSING")

            reconciled = runtime.reconcile(
                operation["id"], f"RECONCILE {operation['id']}"
            )
            self.assertEqual(reconciled["status"], "reconciled")
            self.assertEqual(
                load_instance_locator(runtime.locator_store),
                adoption_request(target).locator,
            )
            self.assertEqual(runtime.runtime_state.read()["enabledPluginApis"], [2])

    def test_fresh_release_mismatch_fails_before_any_adoption_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = manifest()
            runtime, source = self.make_runtime(Path(directory), target)
            different = copy.deepcopy(target)
            different["release"]["commit"] = "f" * 40
            source.verified = different

            with self.assertRaisesRegex(StateError, "differs"):
                runtime.adopt_initial_release(adoption_request(target))

            self.assertEqual(runtime.agent.operations.list(), [])
            self.assertFalse(runtime.runtime_state.path.exists())
            with self.assertRaises(LocatorError):
                load_instance_locator(runtime.locator_store)


if __name__ == "__main__":
    unittest.main()
