from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.agent import UpdateAgent
from updater.errors import RequestRejected
from updater.executor import UpdateExecutor
from updater.plans import PlanStore
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import OperationStore


def manifest(version: str, digit: str):
    channel = "stable" if "-" not in version else "rc"
    return build_manifest(
        version=version,
        channel=channel,
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit * 64,
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        promoted_from=f"{version}-rc.1" if channel == "stable" else None,
    )


class FakeSource:
    def __init__(self, manifests):
        self.manifests = {item["release"]["version"]: item for item in manifests}

    def list_releases(self, channel, refresh=False):
        accepted = {"stable"} if channel == "stable" else {"stable", "rc"}
        if channel == "beta": accepted.add("beta")
        return [
            {"version": version, "channel": item["release"]["channel"], "publishedAt": item["release"]["createdAt"]}
            for version, item in reversed(self.manifests.items())
            if item["release"]["channel"] in accepted
        ]

    def fetch_verified(self, version, updater_version="1.0.0", refresh=False):
        return self.manifests[version]


class FakeDeployment:
    def __init__(self):
        self.calls = []
        self.enabled_plugin_apis = {2}
    def preflight(self, item): self.calls.append("preflight")
    def verify_recent_backup(self): self.calls.append("backup_check")
    def backup_database(self, operation_id): self.calls.append("backup")
    def pull(self, item): self.calls.append("pull")
    def migrate(self, item): self.calls.append("migrate")
    def bootstrap(self, item): self.calls.append("bootstrap")
    def inspect_enabled_plugin_apis(self, item):
        self.calls.append("inspect_plugins")
        return self.enabled_plugin_apis
    def switch(self, item): self.calls.append(f"switch:{item['release']['version']}")
    def verify_health(self, item): self.calls.append("health")


class UpdateAgentTests(unittest.TestCase):
    def make_agent(self, directory, *, runtime_refresh_seconds=30):
        current = manifest("v1.0.0", "1")
        target = manifest("v1.0.1", "2")
        root = Path(directory)
        slots = ReleaseSlots(root / "releases")
        slots.import_current(current)
        runtime = RuntimeState(root / "state")
        runtime.initialize_from_manifest(current, enabled_plugin_apis={2})
        operations = OperationStore(root / "state")
        source = FakeSource([current, target])
        deployment = FakeDeployment()
        executor = UpdateExecutor(
            store=operations,
            slots=slots,
            release_source=source,
            deployment=deployment,
            runtime_state=runtime,
            lock_path=root / "state" / "update.lock",
            updater_version="1.0.0",
        )
        agent = UpdateAgent(
            source=source,
            operations=operations,
            plans=PlanStore(root / "state"),
            slots=slots,
            runtime_state=runtime,
            executor=executor,
            background=False,
            runtime_refresh_seconds=runtime_refresh_seconds,
        )
        return agent, deployment

    def test_plan_and_apply_are_bound_to_exact_verified_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, deployment = self.make_agent(directory)

            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            result = agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })

            self.assertEqual(result["operation"]["status"], "succeeded")
            self.assertIn("switch:v1.0.1", deployment.calls)
            self.assertEqual(agent.dispatch({"operation": "get_status", "params": {}})["current"]["version"], "v1.0.1")

    def test_wrong_confirmation_and_reused_plan_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            with self.assertRaises(RequestRejected):
                agent.dispatch({"operation": "apply_update", "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.0"}})
            agent.dispatch({"operation": "apply_update", "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"}})
            with self.assertRaises(RequestRejected):
                agent.dispatch({"operation": "apply_update", "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"}})

    def test_stable_is_default_and_release_progress_is_real_operation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)

            releases = agent.dispatch({"operation": "list_releases", "params": {"channel": "stable"}})
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            applied = agent.dispatch({"operation": "apply_update", "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"}})
            operation_id = applied["operation"]["id"]
            operation = agent.dispatch({"operation": "get_operation", "params": {"operationId": operation_id}})
            logs = agent.dispatch({"operation": "get_logs", "params": {"operationId": operation_id, "limit": 100}})

            self.assertTrue(all(item["channel"] == "stable" for item in releases["releases"]))
            self.assertEqual(operation["status"], "succeeded")
            self.assertEqual(logs["events"][-1]["status"], "succeeded")

    def test_status_exposes_live_previous_compatibility_for_rollback_ux(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory, runtime_refresh_seconds=0)
            target = agent.source.manifests["v1.0.1"]
            target["compatibility"]["database"]["appAccepts"].append("animemo-db-v2")
            target["compatibility"]["pluginSdk"]["supportedApis"].append(3)
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })
            agent.runtime_state.update(databaseContract="animemo-db-v2")
            agent.executor.deployment.enabled_plugin_apis = {3}

            status = agent.dispatch({"operation": "get_status", "params": {}})

            self.assertFalse(status["previousCompatibility"]["allowed"])
            self.assertEqual(status["previousCompatibility"]["decision"], "unsafe_downgrade")
            self.assertEqual(status["runtime"]["enabledPluginApis"], [3])

    def test_background_executor_exception_is_persisted_in_operation_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            agent.background = True
            agent.executor.apply = lambda operation_id, target, **kwargs: (_ for _ in ()).throw(RuntimeError("worker exploded"))
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})

            applied = agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })
            operation_id = applied["operation"]["id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                operation = agent.dispatch({"operation": "get_operation", "params": {"operationId": operation_id}})
                if operation["status"] != "idle":
                    break
                time.sleep(0.01)

            self.assertEqual(operation["status"], "failed_pre_switch")
            self.assertIn("worker exploded", operation["events"][-1]["detail"])

    def test_second_background_update_is_rejected_before_operation_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            agent.background = True
            started = threading.Event()
            release = threading.Event()
            original = agent.executor.apply

            def slow_apply(operation_id, target, **kwargs):
                started.set()
                release.wait(2)
                return original(operation_id, target, **kwargs)

            agent.executor.apply = slow_apply
            first_plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            second_plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            first = agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": first_plan["planId"], "confirmation": "APPLY v1.0.1"},
            })
            self.assertTrue(started.wait(1))

            with self.assertRaisesRegex(Exception, "Another AniMemo update operation is active"):
                agent.dispatch({
                    "operation": "apply_update",
                    "params": {"planId": second_plan["planId"], "confirmation": "APPLY v1.0.1"},
                })

            self.assertEqual(len(agent.operations.list()), 1)
            self.assertIsNone(agent.plans.get(second_plan["planId"])["consumedAt"])
            release.set()
            operation_id = first["operation"]["id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                operation = agent.operations.get(operation_id)
                if operation["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual(operation["status"], "succeeded")

    def test_background_rollback_returns_operation_before_executor_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })
            agent.background = True
            started = threading.Event()
            release = threading.Event()
            original = agent.executor.rollback

            def slow_rollback(operation_id, previous, **kwargs):
                started.set()
                release.wait(2)
                return original(operation_id, previous, **kwargs)

            agent.executor.rollback = slow_rollback
            before = time.monotonic()
            result = agent.dispatch({
                "operation": "rollback_previous",
                "params": {"confirmation": "ROLLBACK PREVIOUS"},
            })

            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(result["operation"]["status"], "idle")
            self.assertTrue(started.wait(1))
            release.set()
            operation_id = result["operation"]["id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                operation = agent.dispatch({"operation": "get_operation", "params": {"operationId": operation_id}})
                if operation["status"] == "rolled_back":
                    break
                time.sleep(0.01)
            self.assertEqual(operation["status"], "rolled_back")

    def test_second_background_rollback_is_rejected_before_operation_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })
            agent.background = True
            started = threading.Event()
            release = threading.Event()
            original = agent.executor.rollback

            def slow_rollback(operation_id, previous, **kwargs):
                started.set()
                release.wait(2)
                return original(operation_id, previous, **kwargs)

            agent.executor.rollback = slow_rollback
            first = agent.dispatch({
                "operation": "rollback_previous",
                "params": {"confirmation": "ROLLBACK PREVIOUS"},
            })
            self.assertTrue(started.wait(1))

            with self.assertRaisesRegex(Exception, "Another AniMemo update operation is active"):
                agent.dispatch({
                    "operation": "rollback_previous",
                    "params": {"confirmation": "ROLLBACK PREVIOUS"},
                })

            self.assertEqual(len(agent.operations.list()), 2)
            release.set()
            operation_id = first["operation"]["id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                operation = agent.operations.get(operation_id)
                if operation["status"] == "rolled_back":
                    break
                time.sleep(0.01)
            self.assertEqual(operation["status"], "rolled_back")

    def test_background_rollback_exception_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })
            agent.background = True
            agent.executor.rollback = lambda operation_id, previous, **kwargs: (_ for _ in ()).throw(RuntimeError("rollback worker exploded"))

            result = agent.dispatch({
                "operation": "rollback_previous",
                "params": {"confirmation": "ROLLBACK PREVIOUS"},
            })
            operation_id = result["operation"]["id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                operation = agent.dispatch({"operation": "get_operation", "params": {"operationId": operation_id}})
                if operation["status"] != "idle":
                    break
                time.sleep(0.01)

            self.assertEqual(operation["status"], "failed_pre_switch")
            self.assertIn("rollback worker exploded", operation["events"][-1]["detail"])


if __name__ == "__main__":
    unittest.main()
