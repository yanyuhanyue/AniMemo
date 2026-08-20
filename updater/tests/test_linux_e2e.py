from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.contract import build_manifest
from updater.agent import UpdateAgent
from updater.client import UnixAgentClient
from updater.executor import UpdateExecutor
from updater.plans import PlanStore
from updater.runtime import HostAgentRuntime
from updater.runtime_state import RuntimeState
from updater.server import UnixRpcServer
from updater.slots import ReleaseSlots
from updater.state import OperationStore
from updater.transport import ExplicitTransportPolicy


def manifest(
    version: str,
    digit: str,
    *,
    contract: str,
    accepts: list[str],
    migration: bool,
):
    return build_manifest(
        version=version,
        channel="rc",
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        installer_materials_sha256="sha256:" + "f" * 64,
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
        minimum_updater_version="1.0.0",
        database_contract=contract,
        database_accepts=accepts,
        migration_required=migration,
        migration_policy="additive-backward-compatible" if migration else "none",
        application_rollback="conditional" if migration else "safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
    )


class LocalReleaseSource:
    def __init__(self, manifests, *, policy=None, verified=None):
        self.manifests = manifests
        self.transport_policy = policy or ExplicitTransportPolicy.github()
        self.verified = verified if verified is not None else []

    def list_releases(self, channel, *, refresh=False):
        return [
            {
                "version": item["release"]["version"],
                "channel": item["release"]["channel"],
                "publishedAt": item["release"]["createdAt"],
            }
            for item in reversed(list(self.manifests.values()))
        ]

    def fetch_verified(self, version, *, updater_version="1.0.0", refresh=False):
        return self.fetch_verified_materials(
            version,
            updater_version=updater_version,
            refresh=refresh,
        ).manifest

    def fetch_verified_materials(
        self,
        version,
        *,
        updater_version="1.0.0",
        refresh=False,
    ):
        del updater_version
        self.verified.append(
            (self.transport_policy.source.value, version, refresh)
        )
        manifest_value = self.manifests[version]
        return SimpleNamespace(
            manifest=manifest_value,
            identity_digest=manifest_value["images"]["api"]["digest"],
        )


class IsolatedDeployment:
    def __init__(self):
        self.live_version = None
        self.calls = []

    def _call(self, name, manifest=None):
        version = manifest["release"]["version"] if manifest else None
        self.calls.append((name, version))

    def preflight(self, manifest): self._call("preflight", manifest)
    def verify_recent_backup(self): self._call("verify_recent_backup")
    def backup_database(self, operation_id): self.calls.append(("backup_database", operation_id)); return "isolated.sql.gz"
    def pull(self, manifest): self._call("pull", manifest)
    def pull_verified(self, materials, policy):
        del policy
        self._call("pull", materials.manifest)
    def migrate(self, manifest): self._call("migrate", manifest)
    def bootstrap(self, manifest): self._call("bootstrap", manifest)
    def inspect_enabled_plugin_apis(self, manifest): self._call("inspect_enabled_plugin_apis", manifest); return {2}

    def switch(self, manifest, *, live_contracts=None):
        self._call("switch", manifest)
        self.live_version = manifest["release"]["version"]

    def verify_health(self, manifest, *, live_contracts=None):
        self._call("verify_health", manifest)
        if self.live_version != manifest["release"]["version"]:
            raise RuntimeError("isolated application identity is unhealthy")


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Linux Unix Socket evidence runs in the Release Gate")
class LinuxUpdaterE2ETests(unittest.TestCase):
    def test_install_a_update_b_health_and_application_rollback_a_retain_database_b(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = manifest(
                "v1.0.0-rc.1",
                "1",
                contract="animemo-db-v1",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                migration=False,
            )
            second = manifest(
                "v1.1.0-rc.1",
                "2",
                contract="animemo-db-v2",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                migration=True,
            )
            source = LocalReleaseSource({
                first["release"]["version"]: first,
                second["release"]["version"]: second,
            })
            resolver_factory = lambda policy: LocalReleaseSource(
                source.manifests,
                policy=policy,
                verified=source.verified,
            )
            slots = ReleaseSlots(root / "releases")
            slots.import_current(first)
            runtime_state = RuntimeState(root / "state")
            runtime_state.initialize_from_manifest(first, enabled_plugin_apis={2})
            operations = OperationStore(root / "state")
            deployment = IsolatedDeployment()
            deployment.live_version = first["release"]["version"]
            runtime_binding = mock.Mock()
            executor = UpdateExecutor(
                store=operations,
                slots=slots,
                release_source=source,
                deployment=deployment,
                runtime_state=runtime_state,
                runtime_binding=runtime_binding,
                lock_path=root / "state" / "update.lock",
                updater_version="1.0.0",
            )
            agent = UpdateAgent(
                source=source,
                operations=operations,
                plans=PlanStore(root / "state"),
                slots=slots,
                runtime_state=runtime_state,
                executor=executor,
                background=False,
                resolver_factory=resolver_factory,
                transport_policy=source.transport_policy,
            )
            socket_path = root / "run" / "updater.sock"
            server = UnixRpcServer(socket_path, agent)
            client = UnixAgentClient(socket_path)

            def request(operation, params=None):
                ready = threading.Event()
                thread = threading.Thread(target=server.serve_once, kwargs={"ready": ready}, daemon=True)
                thread.start()
                self.assertTrue(ready.wait(5))
                result = client.request(operation, params)
                thread.join(5)
                self.assertFalse(thread.is_alive())
                return result

            initial = request("get_status")
            self.assertEqual(initial["current"]["version"], "v1.0.0-rc.1")
            plan = request(
                "plan_update",
                {"version": "v1.1.0-rc.1", "source": "official-mirror"},
            )
            applied = request(
                "apply_update",
                {"planId": plan["planId"], "confirmation": "APPLY v1.1.0-rc.1"},
            )

            self.assertEqual(applied["operation"]["status"], "succeeded")
            self.assertEqual(deployment.live_version, "v1.1.0-rc.1")
            self.assertEqual(runtime_state.read()["databaseContract"], "animemo-db-v2")
            self.assertEqual(slots.read()["previous"]["release"]["version"], "v1.0.0-rc.1")

            rolled_back = request(
                "rollback_previous",
                {"confirmation": "ROLLBACK PREVIOUS"},
            )

            self.assertEqual(rolled_back["operation"]["status"], "rolled_back")
            self.assertEqual(deployment.live_version, "v1.0.0-rc.1")
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0-rc.1")
            self.assertEqual(slots.read()["previous"]["release"]["version"], "v1.1.0-rc.1")
            self.assertEqual(runtime_state.read()["databaseContract"], "animemo-db-v2")
            self.assertEqual([call[0] for call in deployment.calls].count("migrate"), 1)
            self.assertEqual(
                source.verified[-2:],
                [
                    ("official-mirror", "v1.0.0-rc.1", True),
                    ("official-mirror", "v1.0.0-rc.1", True),
                ],
            )


class RuntimeCompositionTests(unittest.TestCase):
    def test_testing_runtime_accepts_an_exact_resolver_and_transport_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = ExplicitTransportPolicy.official_mirror()
            resolver = LocalReleaseSource({}, policy=policy)

            runtime = HostAgentRuntime.testing(
                app_root=root / "app",
                data_root=root / "data",
                state_root=root / "state",
                socket_path=root / "run" / "updater.sock",
                bootstrap_manifest=root / "bootstrap" / "manifest.json",
                release_resolver=resolver,
                resolver_factory=lambda selected: LocalReleaseSource(
                    {},
                    policy=selected,
                ),
                transport_policy=policy,
            )

            self.assertIs(runtime.agent.source, resolver)
            self.assertEqual(runtime.transport_policy.identity, policy.identity)
            self.assertIs(runtime.agent.executor.release_source, resolver)


if __name__ == "__main__":
    unittest.main()
