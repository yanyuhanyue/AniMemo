from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.errors import StateError
from updater.local_bundle import LocalBundleTransportPolicy
from updater.plans import PlanStore
from updater.transport import ExplicitTransportPolicy


def manifest():
    return build_manifest(
        version="v1.0.0",
        channel="stable",
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + "a" * 64,
        web_digest="sha256:" + "b" * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        installer_materials_sha256="sha256:" + "f" * 64,
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
        promoted_from="v1.0.0-rc.1",
    )


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


def release_binding(source: str = "github") -> dict[str, str]:
    policy = (
        ExplicitTransportPolicy.github()
        if source == "github"
        else ExplicitTransportPolicy.official_mirror()
    )
    return {
        "verifiedReleaseIdentity": "sha256:" + "f" * 64,
        "source": source,
        "transportPolicyIdentity": policy.identity,
    }


def local_release_binding() -> dict[str, object]:
    manifest_bytes = json.dumps(
        manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    execution_unsigned = {
        "schema": "animemo.release-execution-receipt/v1",
        "publicationIdentity": "sha256:" + "5" * 64,
        "publicationExecutionReceiptIdentity": "sha256:" + "6" * 64,
        "signedClaimIdentity": "sha256:" + "7" * 64,
        "signedAt": "2026-08-30T00:00:00Z",
    }
    execution_receipt = {
        **execution_unsigned,
        "identity": "sha256:"
        + hashlib.sha256(
            json.dumps(
                execution_unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    return {
        "source": "local-bundle",
        "transportPolicyIdentity": LocalBundleTransportPolicy().identity,
        "verifiedReleaseIdentity": "sha256:" + "f" * 64,
        "transportIdentity": "sha256:" + "1" * 64,
        "payloadIdentity": "sha256:" + "2" * 64,
        "releaseAttestationIdentity": "sha256:" + "3" * 64,
        "releaseExecutionReceipt": execution_receipt,
        "trustProfileVersion": 1,
        "trustProfileIdentity": "sha256:" + "4" * 64,
        "manifestIdentity": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        "deploymentContractIdentity": (
            "sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df"
        ),
        "apiDigest": "sha256:" + "a" * 64,
        "webDigest": "sha256:" + "b" * 64,
        "postgresDigest": (
            "sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571"
        ),
        "redisDigest": (
            "sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf"
        ),
    }


class PlanStoreTests(unittest.TestCase):
    def test_plan_v3_persists_exact_verified_release_and_transport_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PlanStore(Path(directory))

            plan = store.create(
                manifest(),
                {"target": "v1.0.0"},
                release_binding=release_binding("official-mirror"),
                planning_context_identity="sha256:" + "9" * 64,
            )
            restored = store.get(plan["id"])

            self.assertEqual(restored["schemaVersion"], 3)
            self.assertEqual(
                restored["releaseBinding"],
                release_binding("official-mirror"),
            )

    def test_plan_v3_persists_closed_local_bundle_authority_and_instance_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PlanStore(Path(directory))
            instance_identity = "sha256:" + "9" * 64

            plan = store.create(
                manifest(),
                {
                    "allowed": True,
                    "decision": "safe_switch",
                    "rollbackMode": "safe",
                    "migrationRequired": False,
                    "migrationPolicy": "none",
                    "reasons": [],
                },
                release_binding=local_release_binding(),
                planning_context_identity=instance_identity,
            )
            restored = store.get(plan["id"])

            self.assertEqual(restored["schemaVersion"], 3)
            self.assertEqual(restored["releaseBinding"], local_release_binding())
            self.assertEqual(restored["planningContextIdentity"], instance_identity)

    def test_local_release_execution_receipt_is_closed_and_exact(self):
        for mutation in (
            lambda value: value["releaseExecutionReceipt"].__setitem__(
                "signedAt", "2026-08-30T08:00:00+08:00"
            ),
            lambda value: value["releaseExecutionReceipt"].__setitem__(
                "identity", "sha256:" + "0" * 64
            ),
            lambda value: value["releaseExecutionReceipt"].__setitem__(
                "unexpected", True
            ),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                binding = local_release_binding()
                mutation(binding)
                with self.assertRaises(StateError):
                    PlanStore(Path(directory)).create(
                        manifest(),
                        {"target": "v1.0.0"},
                        release_binding=binding,
                        planning_context_identity="sha256:" + "9" * 64,
                    )

    def test_legacy_or_tampered_plan_binding_fails_closed(self):
        mutations = (
            lambda payload: payload.pop("schemaVersion"),
            lambda payload: payload.__setitem__("unexpectedAuthority", "x"),
            lambda payload: payload["releaseBinding"].__setitem__(
                "verifiedReleaseIdentity",
                "f" * 64,
            ),
            lambda payload: payload["releaseBinding"].__setitem__(
                "transportPolicyIdentity",
                "0" * 64,
            ),
            lambda payload: payload["releaseBinding"].__setitem__(
                "source",
                "local-bundle",
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as directory:
                store = PlanStore(Path(directory))
                plan = store.create(
                    manifest(),
                    {"target": "v1.0.0"},
                    release_binding=release_binding(),
                    planning_context_identity="sha256:" + "9" * 64,
                )
                path = store.root / f"{plan['id']}.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                mutate(payload)
                path.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )

                with self.assertRaises(StateError):
                    store.get(plan["id"])

    def test_plan_store_rejects_a_plans_directory_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            outside = root / "outside"
            state_root.mkdir()
            outside.mkdir()
            link_directory(state_root / "plans", outside)
            store = PlanStore(state_root)

            with self.assertRaisesRegex(StateError, "directory"):
                store.create(
                    manifest(),
                    {"target": "v1.0.0"},
                    release_binding=release_binding(),
                    planning_context_identity="sha256:" + "9" * 64,
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_plan_store_rejects_reading_from_a_plans_directory_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            outside_root = root / "outside"
            plan = PlanStore(outside_root).create(
                manifest(),
                {"target": "v1.0.0"},
                release_binding=release_binding(),
                planning_context_identity="sha256:" + "9" * 64,
            )
            state_root.mkdir()
            link_directory(state_root / "plans", outside_root / "plans")
            store = PlanStore(state_root)

            with self.assertRaisesRegex(StateError, "directory"):
                store.get(plan["id"])

    def test_plan_store_rejects_a_hard_linked_plan_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside_root = root / "outside"
            plan = PlanStore(outside_root).create(
                manifest(),
                {"target": "v1.0.0"},
                release_binding=release_binding(),
                planning_context_identity="sha256:" + "9" * 64,
            )
            state_root = root / "state"
            plans = state_root / "plans"
            plans.mkdir(parents=True)
            (plans / f"{plan['id']}.json").hardlink_to(
                outside_root / "plans" / f"{plan['id']}.json"
            )
            store = PlanStore(state_root)

            with self.assertRaisesRegex(StateError, "file"):
                store.get(plan["id"])


if __name__ == "__main__":
    unittest.main()
