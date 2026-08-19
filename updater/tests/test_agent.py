from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.contract import build_manifest
from updater.agent import UpdateAgent
from updater.errors import RecoveryRequired, RequestRejected, StateError
from updater.executor import UpdateExecutor
from updater.plans import PlanStore
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import OperationStore
from updater.transport import ExplicitTransportPolicy


def manifest(version: str, digit: str):
    channel = "stable" if "-" not in version else "rc"
    return build_manifest(
        version=version,
        channel=channel,
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
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
    def __init__(self, manifests, *, policy=None, events=None):
        self.manifests = (
            manifests
            if isinstance(manifests, dict)
            else {item["release"]["version"]: item for item in manifests}
        )
        self.transport_policy = policy or ExplicitTransportPolicy.github()
        self.events = events if events is not None else []

    def list_releases(self, channel, refresh=False):
        accepted = {"stable"} if channel == "stable" else {"stable", "rc"}
        if channel == "beta": accepted.add("beta")
        return [
            {"version": version, "channel": item["release"]["channel"], "publishedAt": item["release"]["createdAt"]}
            for version, item in reversed(self.manifests.items())
            if item["release"]["channel"] in accepted
        ]

    def fetch_verified(self, version, updater_version="1.0.0", refresh=False):
        return self.fetch_verified_materials(
            version,
            updater_version=updater_version,
            refresh=refresh,
        ).manifest

    def fetch_verified_materials(
        self,
        version,
        updater_version="1.0.0",
        refresh=False,
    ):
        del updater_version
        self.events.append((self.transport_policy.source.value, version, refresh))
        manifest_value = self.manifests[version]
        return SimpleNamespace(
            manifest=manifest_value,
            identity_digest=manifest_value["images"]["api"]["digest"],
        )


class FakeDeployment:
    def __init__(self):
        self.calls = []
        self.enabled_plugin_apis = {2}
    def preflight(self, item): self.calls.append("preflight")
    def verify_recent_backup(self): self.calls.append("backup_check")
    def backup_database(self, operation_id): self.calls.append("backup")
    def pull(self, item): self.calls.append("pull")
    def pull_verified(self, materials, policy):
        del materials, policy
        self.calls.append("pull")
    def migrate(self, item): self.calls.append("migrate")
    def bootstrap(self, item): self.calls.append("bootstrap")
    def inspect_enabled_plugin_apis(self, item):
        self.calls.append("inspect_plugins")
        return self.enabled_plugin_apis
    def switch(self, item, *, live_contracts=None): self.calls.append(f"switch:{item['release']['version']}")
    def verify_health(self, item, *, live_contracts=None): self.calls.append("health")


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
        resolver_factory = lambda policy: FakeSource(
            source.manifests,
            policy=policy,
            events=source.events,
        )
        deployment = FakeDeployment()
        runtime_binding = mock.Mock()
        executor = UpdateExecutor(
            store=operations,
            slots=slots,
            release_source=source,
            deployment=deployment,
            runtime_state=runtime,
            runtime_binding=runtime_binding,
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
            resolver_factory=resolver_factory,
            transport_policy=source.transport_policy,
        )
        return agent, deployment

    @staticmethod
    def enter_manual_recovery(agent):
        operation = agent.operations.create("apply_update", {"version": "v1.0.1"})
        for status in [
            "preflight", "fetching", "verifying", "pulling", "migrating",
            "manual_recovery_required",
        ]:
            agent.operations.transition(operation["id"], status)
        return operation

    def test_manual_recovery_blocks_all_mutations_but_keeps_observation_available(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, deployment = self.make_agent(directory)
            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            blocked = self.enter_manual_recovery(agent)
            deployment.inspect_enabled_plugin_apis = lambda manifest: (_ for _ in ()).throw(
                RuntimeError("live application unavailable")
            )

            status = agent.dispatch({"operation": "get_status", "params": {}})
            logs = agent.dispatch({
                "operation": "get_logs",
                "params": {"operationId": blocked["id"], "limit": 100},
            })

            self.assertEqual(status["recoveryBlock"]["operationId"], blocked["id"])
            self.assertEqual(logs["events"][-1]["status"], "manual_recovery_required")
            for request in [
                {"operation": "plan_update", "params": {"version": "v1.0.1"}},
                {
                    "operation": "apply_update",
                    "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
                },
                {"operation": "rollback_previous", "params": {"confirmation": "ROLLBACK PREVIOUS"}},
            ]:
                with self.subTest(operation=request["operation"]), self.assertRaises(RecoveryRequired):
                    agent.dispatch(request)

            self.assertIsNone(agent.plans.get(plan["planId"])["consumedAt"])

    def test_plan_and_apply_are_bound_to_exact_verified_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, deployment = self.make_agent(directory)

            plan = agent.dispatch({"operation": "plan_update", "params": {"version": "v1.0.1"}})
            result = agent.dispatch({
                "operation": "apply_update",
                "params": {"planId": plan["planId"], "confirmation": "APPLY v1.0.1"},
            })

            self.assertEqual(plan["source"], "github")
            self.assertEqual(
                plan["transportPolicyIdentity"],
                ExplicitTransportPolicy.github().identity,
            )
            self.assertEqual(result["operation"]["status"], "succeeded")
            self.assertIn("switch:v1.0.1", deployment.calls)
            self.assertEqual(agent.dispatch({"operation": "get_status", "params": {}})["current"]["version"], "v1.0.1")

    def test_plan_persists_explicit_mirror_policy_and_apply_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)

            plan = agent.dispatch(
                {
                    "operation": "plan_update",
                    "params": {
                        "version": "v1.0.1",
                        "source": "official-mirror",
                    },
                }
            )
            stored = agent.plans.get(plan["planId"])
            result = agent.dispatch(
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": plan["planId"],
                        "confirmation": "APPLY v1.0.1",
                    },
                }
            )

            policy = ExplicitTransportPolicy.official_mirror()
            self.assertEqual(plan["source"], "official-mirror")
            self.assertEqual(plan["transportPolicyIdentity"], policy.identity)
            self.assertEqual(plan["verifiedReleaseIdentity"], "sha256:" + "2" * 64)
            self.assertEqual(
                stored["releaseBinding"],
                {
                    "source": "official-mirror",
                    "transportPolicyIdentity": policy.identity,
                    "verifiedReleaseIdentity": "sha256:" + "2" * 64,
                },
            )
            self.assertEqual(
                result["operation"]["metadata"]["releaseBinding"],
                stored["releaseBinding"],
            )
            target_fetches = [
                event for event in agent.source.events if event[1] == "v1.0.1"
            ]
            self.assertTrue(target_fetches)
            self.assertEqual(
                {event[0] for event in target_fetches},
                {"official-mirror"},
            )

    def test_failed_explicit_mirror_selection_never_falls_back_to_github(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            selected = []

            def unavailable(policy):
                selected.append(policy.source.value)
                raise StateError("selected mirror unavailable")

            agent.resolver_factory = unavailable
            with self.assertRaisesRegex(StateError, "mirror unavailable"):
                agent.dispatch(
                    {
                        "operation": "plan_update",
                        "params": {
                            "version": "v1.0.1",
                            "source": "official-mirror",
                        },
                    }
                )

            self.assertEqual(selected, ["official-mirror"])
            self.assertEqual(agent.source.events, [])

    def test_restarted_agent_applies_the_plan_with_its_persisted_mirror_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            first_agent, _ = self.make_agent(directory)
            plan = first_agent.dispatch(
                {
                    "operation": "plan_update",
                    "params": {
                        "version": "v1.0.1",
                        "source": "official-mirror",
                    },
                }
            )
            root = Path(directory)
            restarted_source = FakeSource(first_agent.source.manifests)
            deployment = FakeDeployment()
            executor = UpdateExecutor(
                store=first_agent.operations,
                slots=first_agent.slots,
                release_source=restarted_source,
                deployment=deployment,
                runtime_state=first_agent.runtime_state,
                runtime_binding=mock.Mock(),
                lock_path=root / "state" / "update.lock",
                updater_version="1.0.0",
            )
            restarted = UpdateAgent(
                source=restarted_source,
                operations=first_agent.operations,
                plans=PlanStore(root / "state"),
                slots=first_agent.slots,
                runtime_state=first_agent.runtime_state,
                executor=executor,
                background=False,
                resolver_factory=lambda policy: FakeSource(
                    restarted_source.manifests,
                    policy=policy,
                    events=restarted_source.events,
                ),
                transport_policy=restarted_source.transport_policy,
            )

            result = restarted.dispatch(
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": plan["planId"],
                        "confirmation": "APPLY v1.0.1",
                    },
                }
            )

            self.assertEqual(result["operation"]["status"], "succeeded")
            self.assertTrue(restarted_source.events)
            self.assertEqual(
                {event[0] for event in restarted_source.events},
                {"official-mirror"},
            )

    def test_rollback_binds_previous_release_to_the_current_operation_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            plan = agent.dispatch(
                {
                    "operation": "plan_update",
                    "params": {
                        "version": "v1.0.1",
                        "source": "official-mirror",
                    },
                }
            )
            agent.dispatch(
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": plan["planId"],
                        "confirmation": "APPLY v1.0.1",
                    },
                }
            )
            agent.source.events.clear()

            result = agent.dispatch(
                {
                    "operation": "rollback_previous",
                    "params": {"confirmation": "ROLLBACK PREVIOUS"},
                }
            )

            self.assertEqual(result["operation"]["status"], "rolled_back")
            self.assertEqual(
                result["operation"]["metadata"]["releaseBinding"],
                {
                    "source": "official-mirror",
                    "transportPolicyIdentity": (
                        ExplicitTransportPolicy.official_mirror().identity
                    ),
                    "verifiedReleaseIdentity": "sha256:" + "1" * 64,
                },
            )
            self.assertTrue(agent.source.events)
            self.assertEqual(
                {event[0] for event in agent.source.events},
                {"official-mirror"},
            )

    def test_apply_rejects_verified_release_identity_drift_without_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, deployment = self.make_agent(directory)
            plan = agent.dispatch(
                {
                    "operation": "plan_update",
                    "params": {"version": "v1.0.1", "source": "github"},
                }
            )
            path = agent.plans.root / f"{plan['planId']}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["releaseBinding"]["verifiedReleaseIdentity"] = "sha256:" + "9" * 64
            path.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            result = agent.dispatch(
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": plan["planId"],
                        "confirmation": "APPLY v1.0.1",
                    },
                }
            )

            self.assertEqual(result["operation"]["status"], "failed_pre_switch")
            self.assertNotIn("switch:v1.0.1", deployment.calls)

    def test_operation_recovery_rebinds_the_persisted_policy_without_redetection(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            plan = agent.dispatch(
                {
                    "operation": "plan_update",
                    "params": {
                        "version": "v1.0.1",
                        "source": "official-mirror",
                    },
                }
            )
            applied = agent.dispatch(
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": plan["planId"],
                        "confirmation": "APPLY v1.0.1",
                    },
                }
            )
            operation = agent.operations.get(applied["operation"]["id"])
            agent.executor.release_source = agent.source

            agent.bind_operation_resolver(operation)

            self.assertEqual(
                agent.executor.release_source.transport_policy.source.value,
                "official-mirror",
            )

    def test_incomplete_legacy_operation_without_policy_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.make_agent(directory)
            operation = agent.operations.create(
                "apply_update",
                {"version": "v1.0.1", "planId": "a" * 32},
            )
            agent.operations.transition(operation["id"], "preflight")

            with self.assertRaisesRegex(StateError, "explicit migration"):
                agent.recover()

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
