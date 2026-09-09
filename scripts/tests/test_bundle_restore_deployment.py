"""Host/fixture coverage only; no Docker, database server or qualification run."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from durability import backup
from durability.compatibility import CompatibilityOutcome
from durability.restore import RestoreAdapterError
from installer import restore_production
from installer.tests import test_restore_production as restore_fixtures
from scripts.tests import test_production_backup_runtime as backup_fixtures
from updater import deployment as deployment_module
from updater.deployment import (
    BUNDLE_RESTORE_CAPABILITY_PROBE,
    BUNDLE_RESTORE_STAGE_INIT,
    HostPaths,
    ImmutableComposeDeployment,
)
from updater.errors import StateError
from updater.tests import test_deployment as deployment_fixtures

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PATH = "updater/docker-compose.runtime.yml"
CONTAINER_ROOT = "/app/runtime/bundle-restore-staging"


def identity(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def runtime_bytes(*, staged):
    raw = (ROOT / RUNTIME_PATH).read_bytes()
    if staged:
        return raw
    return b"".join(line for line in raw.splitlines(keepends=True)
                    if b"BUNDLE_RESTORE_ROOT" not in line and b"bundle-restore-staging" not in line)


def bind_runtime(materials, root, raw):
    path = root / "runtime.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    entry = {"path": RUNTIME_PATH, "sha256": identity(raw), "size": len(raw), "mode": "0644"}
    materials.deployment_contract["files"][1] = {"path": RUNTIME_PATH, "sha256": identity(raw)}
    materials.deployment_contract["materials"] = [
        row for row in materials.deployment_contract["materials"] if row["path"] != RUNTIME_PATH
    ] + [entry]
    original = materials.material
    materials.material = lambda relative: path if relative == RUNTIME_PATH else original(relative)
    return path


class _GateLoader(yaml.SafeLoader):
    pass


_GateLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))
_GateLoader.add_constructor("!reset", lambda loader, node: None)


class BundleRestoreDeploymentTests(unittest.TestCase):
    def test_compose_mounts_are_private_instance_scoped_and_absent_from_web(self):
        for relative, prefix in (
            (RUNTIME_PATH, "${ANIMEMO_DATA_ROOT:?ANIMEMO_DATA_ROOT is required}"),
            ("deploy/docker-compose.build.yml", "${ANIMEMO_TEST_DATA_ROOT}"),
            ("deploy/docker-compose.upgrade-gate.yml", "${ANIMEMO_DATA_ROOT}"),
        ):
            with self.subTest(relative=relative):
                value = yaml.load((ROOT / relative).read_text(encoding="utf-8"), Loader=_GateLoader)
                for role in ("api", "migration", "bootstrap"):
                    service = value["services"][role]
                    self.assertEqual(service["environment"]["BUNDLE_RESTORE_ROOT"], CONTAINER_ROOT)
                    self.assertEqual(
                        [item for item in service["volumes"] if "bundle-restore-staging" in item],
                        [prefix + "/bundle-restore-staging:" + CONTAINER_ROOT],
                    )
                self.assertFalse(any("bundle-restore-staging" in item
                                     for item in value["services"]["web"].get("volumes", [])))

    def _layout_pair(self, directory):
        def material():
            return SimpleNamespace(
                deployment_contract={
                    "schemaVersion": 2, "profile": "v1.1-instance-scoped", "platform": "linux/amd64",
                    "files": [{"path": "deploy/docker-compose.yml", "sha256": identity(b"base")}, {}],
                    "materials": [{"path": "deploy/docker-compose.yml", "sha256": identity(b"base"), "size": 4, "mode": "0644"}],
                },
                material=lambda _relative: None,
            )
        before, after = material(), material()
        old_path = bind_runtime(before, Path(directory) / "before", runtime_bytes(staged=False))
        new_path = bind_runtime(after, Path(directory) / "after", runtime_bytes(staged=True))
        return before, after, old_path, new_path

    def test_layout_transition_accepts_only_the_three_bound_insertions(self):
        with tempfile.TemporaryDirectory() as directory:
            before, after, _old_path, new_path = self._layout_pair(directory)
            check = restore_production.ProductionRestoreCompatibility
            self.assertFalse(check._same_layout(before, after))
            self.assertTrue(check._bundle_restore_layout_transition(before, after))
            self.assertFalse(restore_production._has_bundle_restore_layout(before))
            self.assertTrue(restore_production._has_bundle_restore_layout(after))
            self.assertFalse(check._bundle_restore_layout_transition(after, before))
            self.assertFalse(check._bundle_restore_layout_transition(after, after))
            original = new_path.read_bytes()
            for changed in (
                original + b"x-unrelated: true\n",
                original.replace(b"/app/runtime/bundle-restore-staging", b"/app/runtime/foreign"),
                original.replace(b"/private:/app/runtime/private", b"/private:/app/runtime/private:ro"),
                original.replace(b"  web:\n", b"  web:\n    privileged: true\n"),
            ):
                with self.subTest(changed=identity(changed)):
                    bind_runtime(after, Path(directory) / "after", changed)
                    self.assertFalse(check._bundle_restore_layout_transition(before, after))
            bind_runtime(after, Path(directory) / "after", original)
            new_path.write_bytes(original + b"# changed after verification\n")
            self.assertFalse(check._bundle_restore_layout_transition(before, after))
            with self.assertRaisesRegex(RestoreAdapterError, "RESTORE_RELEASE_MATERIAL_CHANGED"):
                restore_production._has_bundle_restore_layout(after)

    def test_layout_transition_rejects_other_deploy_material_or_base_compose_changes(self):
        for changed in ("file", "material", "mode"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                before, after, _old, _new = self._layout_pair(directory)
                if changed == "file":
                    after.deployment_contract["files"][0]["sha256"] = identity(b"changed")
                elif changed == "material":
                    after.deployment_contract["materials"][0]["sha256"] = identity(b"changed")
                else:
                    after.deployment_contract["materials"][0]["mode"] = "0777"
                self.assertFalse(restore_production.ProductionRestoreCompatibility._bundle_restore_layout_transition(before, after))

    def _restore_case(self, *, source_v2):
        case = restore_fixtures.ProductionRestorePlanTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        if source_v2:
            case.release_manifest["compatibility"]["database"].update(
                contract="animemo-db-v2", appAccepts=["animemo-db-v1", "animemo-db-v2"],
            )
        bind_runtime(case.releases.materials, case.root / "source-overlay", runtime_bytes(staged=False))
        target, materials, platform = case._forward_target()
        bind_runtime(materials, case.root / "target-overlay", runtime_bytes(staged=True))
        original = backup.BackupSourceIdentity
        def source_identity(**kwargs):
            if source_v2:
                kwargs["database_contract"] = {**kwargs["database_contract"], "id": "animemo-db-v2"}
            return original(**kwargs)
        with mock.patch.object(backup, "BackupSourceIdentity", side_effect=source_identity):
            artifact = case._backup()
        return case, artifact, target, materials, platform

    def test_v1_and_v2_forward_restore_use_explicit_migration_and_original_source_identity(self):
        for source_v2 in (False, True):
            with self.subTest(source_v2=source_v2):
                case, artifact, target, materials, platform = self._restore_case(source_v2=source_v2)
                original = (artifact / backup.MANIFEST_NAME).read_bytes()
                evidence = case._prepare_target(artifact, target, platform)
                plan = case.port._contexts[evidence.operation_id].restore_plan
                self.assertEqual(plan.decision.outcome, CompatibilityOutcome.REQUIRES_UPGRADE)
                self.assertEqual(len(plan.decision.actions), 1)
                action = plan.decision.actions[0]
                self.assertEqual(action.kind, "APPLY_FORWARD_MIGRATION")
                self.assertEqual(action.input_identity["databaseContract"], "animemo-db-v2" if source_v2 else "animemo-db-v1")
                self.assertEqual(action.output_identity["databaseContract"], "animemo-db-v2")
                self.assertEqual(action.input_identity["manifestDigest"], case.release.manifest_digest)
                self.assertEqual((artifact / backup.MANIFEST_NAME).read_bytes(), original)
                case.port.revalidate(evidence)
                materials.manifest["release"]["commit"] = "f" * 40
                with self.assertRaises(Exception):
                    case.port.revalidate(evidence)

    def test_v2_forward_restore_rejects_nonadditive_policy_and_unrelated_layout(self):
        for change in ("policy", "layout"):
            with self.subTest(change=change):
                case, artifact, target, materials, platform = self._restore_case(source_v2=True)
                if change == "policy":
                    materials.manifest["compatibility"]["database"]["migration"]["policy"] = "none"
                else:
                    bind_runtime(materials, case.root / "target-overlay", runtime_bytes(staged=True) + b"x-unrelated: true\n")
                with self.assertRaises(Exception):
                    case._prepare_target(artifact, target, platform)

    def test_restore_bootstrap_invalidates_only_new_target_after_migration(self):
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as directory:
                before, after, _old, _new = self._layout_pair(directory)
                events = []
                release = object()
                plan = SimpleNamespace(release=release)
                target = after if staged else before
                deployment = SimpleNamespace(
                    migrate=lambda _manifest: events.append("migrate"),
                    invalidate_restored_bundle_sessions=lambda _manifest: events.append("invalidate"),
                )
                mutation = restore_production.ProductionRestoreMutation.__new__(restore_production.ProductionRestoreMutation)
                mutation.installation_plan = plan
                mutation.reconfigure_names = ()
                mutation.fresh = SimpleNamespace(
                    deployment_for=lambda _plan: deployment, manifest_for=lambda _plan: {},
                    releases=SimpleNamespace(materials_for=lambda _release: target),
                    bootstrap=lambda _plan: events.append("bootstrap"),
                )
                mutation.apply_upgrade((SimpleNamespace(order=1),))
                mutation.bootstrap()
                self.assertEqual(events, ["migrate", "invalidate", "bootstrap"] if staged else ["migrate", "bootstrap"])
                if staged:
                    events.clear()
                    deployment.invalidate_restored_bundle_sessions = mock.Mock(side_effect=StateError("schema missing"))
                    with self.assertRaisesRegex(RestoreAdapterError, "RESTORE_BOOTSTRAP_FAILED"):
                        mutation.bootstrap()
                    self.assertEqual(events, [])

    def test_exact_command_and_applied_0009_are_required_before_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            case = deployment_fixtures.ImmutableComposeDeploymentTests("runTest")
            deployment, _runner, _probes = case.make(directory)
            target = deployment_fixtures.manifest()
            for capability in ({"command": False, "schema": False}, {"command": True, "schema": False}, {"command": 1, "schema": 1}, {"command": True, "schema": True}):
                with self.subTest(capability=capability):
                    deployment._compose = mock.Mock(return_value=SimpleNamespace(stdout=json.dumps(capability)))
                    if all(value is True for value in capability.values()):
                        deployment.invalidate_restored_bundle_sessions(target)
                        self.assertEqual(deployment._compose.call_count, 2)
                        self.assertEqual(deployment._compose.call_args.args[-1], "invalidate_restored_bundle_sessions")
                    else:
                        with self.assertRaises(StateError):
                            deployment.invalidate_restored_bundle_sessions(target)
                        self.assertEqual(deployment._compose.call_count, 1)
                    self.assertIn(BUNDLE_RESTORE_CAPABILITY_PROBE, deployment._compose.call_args_list[0].args)

    def test_capability_probe_binds_exact_migration_and_live_table(self):
        for applied, table_present, command_owner in ((True, True, "journal"), (False, True, "journal"), (True, False, "journal"), (True, True, "foreign")):
            with self.subTest(applied=applied, table_present=table_present, command_owner=command_owner):
                query = mock.Mock(return_value=SimpleNamespace(exists=lambda: applied))
                model = SimpleNamespace(_meta=SimpleNamespace(db_table="journal_bundlerestoresession"))
                modules = {
                    "django.apps": SimpleNamespace(apps=SimpleNamespace(get_model=lambda app, name: model)),
                    "django.core.management": SimpleNamespace(get_commands=lambda: {"invalidate_restored_bundle_sessions": command_owner}),
                    "django.db": SimpleNamespace(connection=SimpleNamespace(introspection=SimpleNamespace(table_names=lambda: [model._meta.db_table] if table_present else []))),
                    "django.db.migrations.recorder": SimpleNamespace(MigrationRecorder=SimpleNamespace(Migration=SimpleNamespace(objects=SimpleNamespace(filter=query)))),
                }
                output = io.StringIO()
                with mock.patch.dict(sys.modules, modules), contextlib.redirect_stdout(output):
                    exec(BUNDLE_RESTORE_CAPABILITY_PROBE, {})
                self.assertEqual(json.loads(output.getvalue()), {"command": command_owner == "journal", "schema": command_owner == "journal" and applied and table_present})
                if command_owner == "journal":
                    query.assert_called_once_with(app="journal", name="0009_bundle_restore_sessions")
                else:
                    query.assert_not_called()

    def test_fixed_directory_helper_is_only_instance_bound_and_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, data, state_root = root / "app", root / "data", root / "state"
            for path in (app, data, state_root):
                path.mkdir()
            target = deployment_fixtures.manifest()
            target["compatibility"]["database"]["contract"] = "animemo-db-v2"
            stage = data / "bundle-restore-staging"
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                stage.mkdir(mode=0o700)
                return SimpleNamespace(stdout=json.dumps({"created": True, "uid": 10001, "gid": 10001, "mode": "0700"}))
            deployment = ImmutableComposeDeployment(HostPaths.testing(app=app, data=data, state=state_root), runner=SimpleNamespace(run=run))
            original_lstat = Path.lstat
            def owned_metadata(path, *args, **kwargs):
                value = original_lstat(path, *args, **kwargs)
                if path != data:
                    return value
                return SimpleNamespace(st_mode=value.st_mode & ~0o022, st_uid=0, st_file_attributes=0)
            with mock.patch.object(Path, "lstat", owned_metadata), mock.patch.object(deployment, "_validate_bundle_restore_staging") as validate:
                deployment.prepare_bundle_restore_staging(target)
                deployment.prepare_bundle_restore_staging(target)
            self.assertEqual(len(calls), 1)
            argv, kwargs = calls[0]
            self.assertEqual(argv[:5], ["/usr/bin/docker", "run", "--pull", "never", "--rm"])
            self.assertIn("--read-only", argv)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertEqual(argv[argv.index("--mount") + 1], f"type=bind,source={data},target=/animemo-instance-data")
            self.assertIn("io.animemo.instance-name=default", argv)
            self.assertIn("io.animemo.instance-id=00000000-0000-4000-8000-000000000000", argv)
            self.assertIn("io.animemo.compose-project=animemo-default", argv)
            self.assertNotIn("DATABASE_URL", kwargs["env"])
            self.assertEqual(argv[-1], BUNDLE_RESTORE_STAGE_INIT)
            self.assertIn("ghcr.io/yanyuhanyue/animemo-api@sha256:", argv[-3])
            self.assertEqual(kwargs["timeout"], 60)
            self.assertEqual(validate.call_count, 2)

    def test_stage_validation_rejects_files_reparse_points_and_wrong_posix_owner(self):
        path = Path("stage-fixture")
        for mode, uid, gid, flags in (
            (stat.S_IFREG | 0o600, 10001, 10001, 0),
            (stat.S_IFDIR | 0o700, 10001, 10001, 0x400),
            (stat.S_IFDIR | 0o700, 0, 0, 0),
            (stat.S_IFDIR | 0o755, 10001, 10001, 0),
        ):
            value = SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_file_attributes=flags)
            with self.subTest(mode=mode, uid=uid, flags=flags), mock.patch.object(Path, "lstat", return_value=value), mock.patch.object(Path, "is_symlink", return_value=False), mock.patch.object(deployment_module.os, "name", "posix"):
                with self.assertRaises(OSError):
                    ImmutableComposeDeployment._validate_bundle_restore_staging(path)

    def test_backup_excludes_unfinished_stage_and_preserves_original_staging_bytes(self):
        case = backup_fixtures.ProductionBackupRuntimeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        stage = case.data / "bundle-restore-staging" / "session-fixture"
        stage.mkdir(parents=True)
        marker = b"PORTABLE_STAGE_PRIVATE_CANARY"
        (stage / "chunk-0000000000000000").write_bytes(marker)
        _plan, receipt, _ = case.create()
        artifact = Path(receipt.path)
        manifest = json.loads((artifact / backup.MANIFEST_NAME).read_bytes())
        self.assertFalse(any("bundle-restore-staging" in item["path"] for item in manifest["filesystem"]["members"]))
        self.assertEqual((stage / "chunk-0000000000000000").read_bytes(), marker)
        self.assertFalse(any(path.name == "chunk-0000000000000000" for path in artifact.rglob("*")))


if __name__ == "__main__":
    unittest.main()
