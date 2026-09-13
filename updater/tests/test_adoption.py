from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from dataclasses import replace

from durability.instance import (
    LocatorError,
    load_instance_locator,
    parse_instance_locator,
    release_identity_from_manifest,
)
from scripts.tests.test_durability_instance import locator_payload
from updater.errors import StateError
from installer.production import LocalDockerCommandRunner
from updater.deployment import CANDIDATE_NETWORK_OVERRIDE_TEXT, HostPaths, ImmutableComposeDeployment
from updater.runtime import HostAgentRuntime, InitialAdoptionRequest
from updater.tests.test_deployment import FakeRunner, manifest


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

    def inspect_enabled_plugin_apis(self, target, *, running=False):
        if not running:
            raise StateError('Adoption must observe the verified running API')
        return {2}


def adoption_request(target: dict[str, object]) -> InitialAdoptionRequest:
    payload = locator_payload()
    payload["releaseIdentity"] = dict(release_identity_from_manifest(target))
    return InitialAdoptionRequest(
        locator=parse_instance_locator(payload),
        manifest=copy.deepcopy(target),
    )


class InitialAdoptionTests(unittest.TestCase):
    def test_adoption_keeps_installer_network_and_records_only_running_api_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = HostPaths.testing(app=root / 'app', data=root / 'data', state=root / 'state')
            private = paths.data_root / 'private'
            private.mkdir(parents=True, mode=0o700)
            override = private / 'candidate-network-isolation.yml'
            override.write_text(CANDIDATE_NETWORK_OVERRIDE_TEXT, encoding='utf-8')
            override.chmod(0o600)
            delegate = FakeRunner()
            runner = LocalDockerCommandRunner(delegate)
            deployment = ImmutableComposeDeployment(paths, runner=runner, candidate_network_override=override)
            runtime = HostAgentRuntime._build(paths=paths, socket_path=root / 'run/updater.sock',
                bootstrap_manifest=root / 'unused.json', background=False, deployment=deployment)
            target = manifest()
            contracts = {key + 'Contract': target['compatibility'][key]['contract']
                         for key in ('database', 'configuration')}
            with (mock.patch.object(deployment, 'verify_deployment_contract'),
                  mock.patch.object(deployment, 'verify_health'),
                  mock.patch.object(deployment, 'inspect_runtime_contracts', return_value=contracts)):
                receipt = runtime.adopt_initial_release(adoption_request(target), verifier=lambda _: target)
            self.assertIs(runtime.deployment, deployment)
            self.assertIs(runtime.agent.executor.deployment, deployment)
            self.assertEqual(runtime.agent.operations.get(receipt.operation_id)['status'], 'succeeded')
            self.assertEqual(len(delegate.calls), 1)
            argv, _ = delegate.calls[0]
            self.assertEqual(argv[:3], ('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock'))
            self.assertIn(str(override), argv)
            self.assertEqual(argv[-6:], ('exec', '-T', 'api', 'python', 'manage.py', 'list_enabled_plugin_apis'))
            self.assertEqual(len(runner.completed_commands), 1)
            self.assertEqual(runtime.runtime_state.read()['enabledPluginApis'], [2])

    def test_adoption_deployment_must_match_runtime_paths_and_managed_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = HostPaths.testing(app=root / 'app', data=root / 'data', state=root / 'state')
            invalid = (object(), ImmutableComposeDeployment(replace(paths, instance_id='different')),
                       ImmutableComposeDeployment(paths, managed_environment={'CONFIG': 'different'}))
            for deployment in invalid:
                with self.subTest(deployment=type(deployment).__name__), self.assertRaisesRegex(StateError, 'differs from the bound runtime'):
                    HostAgentRuntime._build(paths=paths, socket_path=root / 'run/updater.sock',
                        bootstrap_manifest=root / 'unused.json', background=False, deployment=deployment)

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

    def test_installer_bound_verifier_avoids_a_different_public_release_source(self):
        with tempfile.TemporaryDirectory() as directory:
            target = manifest()
            runtime, source = self.make_runtime(Path(directory), target)
            requests: list[str] = []

            def verifier(version: str) -> dict[str, object]:
                requests.append(version)
                return copy.deepcopy(target)

            runtime.adopt_initial_release(
                adoption_request(target),
                verifier=verifier,
            )

            self.assertEqual(requests, ["v1.0.0"])
            self.assertEqual(source.requests, [])
            self.assertEqual(runtime.slots.read()["current"], target)

    def test_installer_bound_verifier_must_return_the_exact_adoption_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target = manifest()
            runtime, _ = self.make_runtime(Path(directory), target)
            different = copy.deepcopy(target)
            different["release"]["commit"] = "f" * 40

            with self.assertRaisesRegex(StateError, "differs"):
                runtime.adopt_initial_release(
                    adoption_request(target),
                    verifier=lambda _version: different,
                )

            self.assertEqual(runtime.agent.operations.list(), [])

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
                set(reconciled),
                {"id", "kind", "status", "createdAt", "updatedAt", "events"},
            )
            self.assertNotIn("metadata", reconciled)
            self.assertNotIn("recovery", reconciled)
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
