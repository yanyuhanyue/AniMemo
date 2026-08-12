from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import (
    ReleaseContractError,
    assert_tag_absent,
    build_provenance_plan,
    build_manifest,
    promote_manifest,
    previous_stable_tag,
    resolve_prerelease,
    validate_manifest,
)


COMMIT = "a" * 40
API_DIGEST = "sha256:" + "1" * 64
WEB_DIGEST = "sha256:" + "2" * 64


def manifest(**overrides):
    values = {
        "version": "v1.0.0-rc.1",
        "channel": "rc",
        "commit": COMMIT,
        "created_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
        "api_digest": API_DIGEST,
        "web_digest": WEB_DIGEST,
        "minimum_updater_version": "1.0.0",
        "database_contract": "animemo-db-v1",
        "database_accepts": ["animemo-db-v1"],
        "migration_required": False,
        "migration_policy": "none",
        "application_rollback": "safe",
        "configuration_contract": "animemo-config-v1",
        "configuration_accepts": ["animemo-config-v1"],
        "plugin_sdk_apis": [2],
    }
    values.update(overrides)
    return build_manifest(**values)


class VersionResolutionTests(unittest.TestCase):
    def test_initial_release_line_requires_explicit_bootstrap_version(self):
        with self.assertRaisesRegex(ReleaseContractError, "target-version-override"):
            resolve_prerelease(tags=[], bump="patch", channel="rc")

        plan = resolve_prerelease(
            tags=[],
            bump="patch",
            channel="rc",
            target_version_override="v1.0.0",
        )
        self.assertEqual(plan, {"targetVersion": "v1.0.0", "releaseTag": "v1.0.0-rc.1", "sequence": 1})

    def test_existing_release_line_uses_bump_and_next_channel_sequence(self):
        plan = resolve_prerelease(
            tags=["v1.0.0", "v1.0.1-beta.1", "v1.0.1-beta.2", "v1.0.1-rc.10"],
            bump="patch",
            channel="rc",
        )
        self.assertEqual(plan["targetVersion"], "v1.0.1")
        self.assertEqual(plan["releaseTag"], "v1.0.1-rc.11")
        self.assertEqual(plan["sequence"], 11)

    def test_bootstrap_override_is_rejected_after_a_stable_tag_exists(self):
        with self.assertRaisesRegex(ReleaseContractError, "bootstrap"):
            resolve_prerelease(
                tags=["v1.0.0"],
                bump="minor",
                channel="beta",
                target_version_override="v1.1.0",
            )

    def test_invalid_or_existing_target_tag_is_rejected(self):
        with self.assertRaises(ReleaseContractError):
            resolve_prerelease(tags=[], bump="patch", channel="beta", target_version_override="1.0.0")
        with self.assertRaisesRegex(ReleaseContractError, "already exists"):
            assert_tag_absent("v1.0.0-rc.1", ["v1.0.0-rc.1"])

    def test_previous_stable_ignores_prereleases_and_target(self):
        self.assertEqual(
            previous_stable_tag(
                ["v1.0.0", "v1.1.0-beta.1", "v1.1.0", "v1.2.0-rc.1"],
                target="v1.2.0",
            ),
            "v1.1.0",
        )
        self.assertIsNone(previous_stable_tag(["v1.0.0-rc.1"], target="v1.0.0"))


class ManifestContractTests(unittest.TestCase):
    def test_valid_manifest_round_trips_through_versioned_schema(self):
        payload = manifest()
        validate_manifest(payload, updater_version="1.0.0")
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["images"]["api"]["digest"], API_DIGEST)
        self.assertEqual(payload["compatibility"]["pluginSdk"]["supportedApis"], [2])
        self.assertNotIn("tag", payload["images"]["api"])

    def test_manifest_rejects_missing_or_mutable_image_identity(self):
        missing = manifest()
        del missing["images"]["api"]["digest"]
        with self.assertRaises(ReleaseContractError):
            validate_manifest(missing)

        mutable = manifest()
        mutable["images"]["api"]["tag"] = "latest"
        with self.assertRaises(ReleaseContractError):
            validate_manifest(mutable)

    def test_manifest_rejects_invalid_commit_schema_and_compatibility(self):
        invalid_commit = manifest()
        invalid_commit["release"]["commit"] = "abc123"
        with self.assertRaises(ReleaseContractError):
            validate_manifest(invalid_commit)

        unsupported = manifest()
        unsupported["schemaVersion"] = 2
        with self.assertRaisesRegex(ReleaseContractError, "schemaVersion"):
            validate_manifest(unsupported)

        incompatible = manifest()
        incompatible["compatibility"]["database"]["migration"] = {
            "required": True,
            "policy": "none",
        }
        with self.assertRaisesRegex(ReleaseContractError, "migration"):
            validate_manifest(incompatible)

    def test_manifest_rejects_updater_older_than_declared_minimum(self):
        with self.assertRaisesRegex(ReleaseContractError, "updater"):
            validate_manifest(manifest(minimum_updater_version="1.2.0"), updater_version="1.1.9")

    def test_stable_manifest_can_only_promote_exact_rc_artifacts(self):
        rc = manifest()
        stable = promote_manifest(rc, existing_tags=["v0.9.0"])
        validate_manifest(stable)
        self.assertEqual(stable["release"]["version"], "v1.0.0")
        self.assertEqual(stable["release"]["promotedFrom"], "v1.0.0-rc.1")
        self.assertEqual(stable["release"]["commit"], rc["release"]["commit"])
        self.assertEqual(stable["images"], rc["images"])

        with self.assertRaisesRegex(ReleaseContractError, "already exists"):
            promote_manifest(rc, existing_tags=["v1.0.0"])

        beta = manifest(version="v1.0.0-beta.1", channel="beta")
        with self.assertRaisesRegex(ReleaseContractError, "RC"):
            promote_manifest(beta, existing_tags=[])

    def test_manifest_file_is_canonical_utf8_json(self):
        payload = manifest()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "release-manifest.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(loaded, payload)


class ProvenancePlanTests(unittest.TestCase):
    def test_dry_run_plan_binds_both_images_to_commit_and_workflow(self):
        plan = build_provenance_plan(
            version="v1.0.0-rc.1",
            commit=COMMIT,
            api_digest=API_DIGEST,
            web_digest=WEB_DIGEST,
            created_at="2026-08-12T10:00:00Z",
        )
        self.assertEqual(plan["predicateType"], "https://slsa.dev/provenance/v1")
        self.assertEqual(plan["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"], COMMIT)
        self.assertEqual(
            plan["subject"],
            [
                {"name": "ghcr.io/yanyuhanyue/animemo-api", "digest": {"sha256": "1" * 64}},
                {"name": "ghcr.io/yanyuhanyue/animemo-web", "digest": {"sha256": "2" * 64}},
            ],
        )

    def test_dry_run_plan_rejects_mutable_or_malformed_identity(self):
        with self.assertRaises(ReleaseContractError):
            build_provenance_plan(
                version="latest",
                commit=COMMIT,
                api_digest=API_DIGEST,
                web_digest=WEB_DIGEST,
                created_at="2026-08-12T10:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
