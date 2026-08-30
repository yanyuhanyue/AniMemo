import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from django.contrib.auth import get_user_model
from django.core.management import ManagementUtility, call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase, override_settings

from plugin_host.models import (
    PluginData,
    PluginDeployment,
    PluginPackageBlob,
    PluginProject,
    PluginVersion,
    UserPluginInstallation,
)
from plugin_host.official_packages import (
    ZIP_TIMESTAMP,
    build_official_package,
    canonical_content_digest_from_package,
    canonical_content_digest_from_source,
)
from plugin_host.package import PluginPackageError
from plugin_host.runtime import runtime_registry
from plugin_host.services import store_package_blob

HISTORICAL_OFFICIAL_RELEASE_SHA = "347f3e8466e7fe67296aecabe0d68fc958f729e4"


def _repack(payload, compression):
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", compression) as target:
        for source_info in source.infolist():
            if source_info.is_dir():
                continue
            info = ZipInfo(source_info.filename, date_time=ZIP_TIMESTAMP)
            info.compress_type = compression
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            target.writestr(info, source.read(source_info))
    return output.getvalue()


class OfficialPluginSyncTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name), PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings.enable()
        get_user_model().objects.create_superuser(
            username="official-sync-admin",
            email="official-sync@example.com",
            password="temporary-password-123",
        )

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    def test_official_package_is_deterministic(self):
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        first = build_official_package(source)
        second = build_official_package(source)

        self.assertEqual(first, second)
        with ZipFile(BytesIO(first)) as archive:
            self.assertTrue(archive.infolist())
            self.assertTrue(all(item.date_time == ZIP_TIMESTAMP for item in archive.infolist()))

    def test_official_package_rejects_a_linked_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "official-plugin"
            source.mkdir()
            outside = Path(directory) / "manifest.json"
            outside.write_text('{"id":"outside"}\n', encoding="utf-8")
            linked = source / "manifest.json"
            try:
                linked.symlink_to(outside)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("symlink creation is unavailable on this Windows host")
                raise

            with self.assertRaisesRegex(RuntimeError, "must not contain links"):
                build_official_package(source)

    def test_official_package_rejects_file_replacement_during_open(self):
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        with tempfile.TemporaryDirectory() as directory:
            replacement = Path(directory) / "replacement.json"
            replacement.write_bytes((source / "manifest.json").read_bytes())
            real_open = os.open

            def replace_manifest(path, flags, *args):
                selected = replacement if Path(path).name == "manifest.json" else path
                return real_open(selected, flags, *args)

            with (
                patch(
                    "plugin_host.official_packages.os.open",
                    side_effect=replace_manifest,
                ),
                self.assertRaisesRegex(RuntimeError, "changed while opening"),
            ):
                build_official_package(source)

    def test_canonical_content_identity_is_independent_of_archive_compression(self):
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        archive_a = build_official_package(source)
        archive_b = _repack(archive_a, ZIP_STORED)

        self.assertNotEqual(hashlib.sha256(archive_a).hexdigest(), hashlib.sha256(archive_b).hexdigest())
        self.assertEqual(
            canonical_content_digest_from_package(archive_a),
            canonical_content_digest_from_package(archive_b),
        )
        self.assertEqual(
            canonical_content_digest_from_source(source),
            canonical_content_digest_from_package(archive_a),
        )

    def test_official_canonical_content_digest_matches_git_release_identity(self):
        repository = Path(__file__).resolve().parents[3]
        archive = subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                HISTORICAL_OFFICIAL_RELEASE_SHA,
                "--",
                "plugins/watch-history-importer",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        with tempfile.TemporaryDirectory() as temporary:
            with tarfile.open(fileobj=BytesIO(archive), mode="r:") as package:
                package.extractall(temporary, filter="data")
            source = Path(temporary) / "plugins" / "watch-history-importer"
            content_digest = canonical_content_digest_from_source(source)

        self.assertEqual(content_digest, "02ae932d7de62d5a8a2b439ba5753251d0a81a051252e49eb80a886b7568305a")

    @unittest.skipIf(os.name == "nt", "发布 ZIP SHA 在 Linux CI/生产镜像中验证；Windows zlib 压缩字节不同")
    def test_historical_official_archive_matches_immutable_release_sha(self):
        repository = Path(__file__).resolve().parents[3]
        archive = subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                HISTORICAL_OFFICIAL_RELEASE_SHA,
                "--",
                "plugins/watch-history-importer",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        with tempfile.TemporaryDirectory() as temporary:
            with tarfile.open(fileobj=BytesIO(archive), mode="r:") as package:
                package.extractall(temporary, filter="data")
            source = Path(temporary) / "plugins" / "watch-history-importer"
            package_sha = hashlib.sha256(build_official_package(source)).hexdigest()

        self.assertEqual(package_sha, "2e55570a3a78867646d05b39543cfc63b46a0fdeef884c832c9e15c8f5fbdf05")

    def test_sync_is_idempotent(self):
        call_command("sync_official_plugins")
        deployment = PluginDeployment.objects.get(plugin__slug="watch-history-importer")
        first_version_id = deployment.current_version_id
        first_blob_id = deployment.current_version.package_blob_id

        call_command("sync_official_plugins")
        deployment.refresh_from_db()

        self.assertEqual(deployment.current_version_id, first_version_id)
        self.assertEqual(deployment.current_version.package_blob_id, first_blob_id)
        self.assertEqual(PluginProject.objects.count(), 1)
        self.assertEqual(PluginVersion.objects.count(), 1)
        self.assertEqual(PluginPackageBlob.objects.count(), 1)

    def test_sync_migrates_the_legacy_official_id_without_replacing_the_project(self):
        user = get_user_model().objects.create_user(username="legacy-official-plugin-user")
        project = PluginProject.objects.create(
            plugin_id="com.anime-journal.watch-history-importer",
            slug="watch-history-importer",
            name="Legacy importer",
            description="Historical official plugin identity",
        )
        installation = UserPluginInstallation.objects.create(
            user=user,
            plugin=project,
            enabled=True,
            config={"preserve": True},
        )
        plugin_data = PluginData.objects.create(
            user=user,
            plugin=project,
            namespace="fixture",
            key="preserve",
            value={"still": "owned"},
        )
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        package_payload = build_official_package(source)
        blob, _, _, _ = store_package_blob(package_payload)
        legacy_version = PluginVersion.objects.create(
            plugin=project,
            version="0.4.2",
            package_blob=blob,
            manifest_snapshot={
                "id": "com.anime-journal.watch-history-importer",
                "slug": "watch-history-importer",
                "version": "0.4.2",
            },
            runtime_types=["frontend", "backend"],
            review_status=PluginVersion.ReviewStatus.APPROVED,
        )

        call_command("sync_official_plugins")

        project.refresh_from_db()
        installation.refresh_from_db()
        plugin_data.refresh_from_db()
        legacy_version.refresh_from_db()
        self.assertEqual(project.plugin_id, "com.animemo.watch-history-importer")
        self.assertEqual(PluginProject.objects.count(), 1)
        self.assertEqual(installation.plugin_id, project.pk)
        self.assertEqual(plugin_data.plugin_id, project.pk)
        self.assertEqual(legacy_version.plugin_id, project.pk)

    def test_sync_rejects_an_unknown_project_using_the_official_slug(self):
        project = PluginProject.objects.create(
            plugin_id="com.example.unrelated-importer",
            slug="watch-history-importer",
            name="Unrelated importer",
            description="Must not be claimed by official synchronization",
        )

        with self.assertRaisesRegex(
            CommandError,
            r"^official_plugin_sync_failed correlation_id=[0-9a-f]{32}$",
        ):
            call_command("sync_official_plugins")

        project.refresh_from_db()
        self.assertEqual(project.plugin_id, "com.example.unrelated-importer")
        self.assertEqual(PluginProject.objects.count(), 1)

    def test_sync_retains_existing_blob_for_same_content_with_different_archive(self):
        call_command("sync_official_plugins")
        deployment = PluginDeployment.objects.select_related("current_version__package_blob").get(
            plugin__slug="watch-history-importer"
        )
        version_id = deployment.current_version_id
        blob_id = deployment.current_version.package_blob_id
        blob_sha = deployment.current_version.package_blob.sha256
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        alternate = _repack(build_official_package(source), ZIP_STORED)
        alternate_sha = hashlib.sha256(alternate).hexdigest()
        self.assertNotEqual(alternate_sha, blob_sha)

        with patch(
            "plugin_host.management.commands.sync_official_plugins.build_official_package",
            return_value=alternate,
        ), patch("plugin_host.management.commands.sync_official_plugins.PluginPackageInstaller.publish") as publish:
            call_command("sync_official_plugins")

        deployment.refresh_from_db()
        self.assertEqual(deployment.current_version_id, version_id)
        self.assertEqual(deployment.current_version.package_blob_id, blob_id)
        self.assertEqual(deployment.current_version.package_blob.sha256, blob_sha)
        self.assertEqual(PluginVersion.objects.count(), 1)
        self.assertEqual(PluginPackageBlob.objects.count(), 1)
        self.assertFalse((Path(self.root.name) / "packages" / "sha256" / alternate_sha[:2] / f"{alternate_sha}.ajplugin").exists())
        publish.assert_not_called()

    def test_sync_rejects_true_content_change_before_storing_blob(self):
        call_command("sync_official_plugins")
        deployment = PluginDeployment.objects.select_related("current_version__package_blob").get(
            plugin__slug="watch-history-importer"
        )
        original_version_id = deployment.current_version_id
        original_blob_id = deployment.current_version.package_blob_id
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        with tempfile.TemporaryDirectory() as temporary:
            changed_source = Path(temporary) / "watch-history-importer"
            shutil.copytree(source, changed_source)
            (changed_source / "frontend" / "plugin.js").write_text("changed immutable payload", encoding="utf-8")
            changed_package = build_official_package(changed_source)
        changed_sha = hashlib.sha256(changed_package).hexdigest()

        with patch(
            "plugin_host.management.commands.sync_official_plugins.build_official_package",
            return_value=changed_package,
        ), patch(
            "plugin_host.management.commands.sync_official_plugins.PluginPackageInstaller.publish"
        ) as publish, self.assertRaisesRegex(
            CommandError,
            r"^official_plugin_sync_failed correlation_id=[0-9a-f]{32}$",
        ):
            call_command("sync_official_plugins")

        deployment.refresh_from_db()
        self.assertEqual(deployment.current_version_id, original_version_id)
        self.assertEqual(deployment.current_version.package_blob_id, original_blob_id)
        self.assertEqual(PluginVersion.objects.count(), 1)
        self.assertEqual(PluginPackageBlob.objects.count(), 1)
        self.assertFalse((Path(self.root.name) / "packages" / "sha256" / changed_sha[:2] / f"{changed_sha}.ajplugin").exists())
        publish.assert_not_called()

    def test_sync_fails_closed_when_historical_cas_blob_is_missing(self):
        call_command("sync_official_plugins")
        version = PluginVersion.objects.select_related("package_blob").get(plugin__slug="watch-history-importer")
        package_path = Path(self.root.name) / version.package_blob.storage_path
        package_path.unlink()

        with self.assertRaisesRegex(
            CommandError,
            r"^official_plugin_sync_failed correlation_id=[0-9a-f]{32}$",
        ):
            call_command("sync_official_plugins")

        self.assertEqual(PluginVersion.objects.count(), 1)
        self.assertEqual(PluginPackageBlob.objects.count(), 1)

    def test_sync_fails_closed_when_historical_cas_blob_is_corrupt(self):
        call_command("sync_official_plugins")
        version = PluginVersion.objects.select_related("package_blob").get(plugin__slug="watch-history-importer")
        package_path = Path(self.root.name) / version.package_blob.storage_path
        package_path.write_bytes(json.dumps({"corrupt": True}).encode("utf-8"))

        with self.assertRaisesRegex(
            CommandError,
            r"^official_plugin_sync_failed correlation_id=[0-9a-f]{32}$",
        ):
            call_command("sync_official_plugins")

        self.assertEqual(PluginVersion.objects.count(), 1)
        self.assertEqual(PluginPackageBlob.objects.count(), 1)

    def test_sync_package_failure_does_not_expose_hostile_diagnostics(self):
        sentinels = (
            r"C:\Users\hostile-user\private.sql",
            "SELECT secret FROM auth_user",
            "official-package-secret-sentinel",
            "hostile-user",
        )
        errors = StringIO()
        with (
            patch(
                "plugin_host.management.commands.sync_official_plugins.build_official_package",
                side_effect=PluginPackageError(" ".join(sentinels)),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            ManagementUtility(
                ["manage.py", "sync_official_plugins", "--no-color"]
            ).execute()

        public_text = errors.getvalue()
        for sentinel in sentinels:
            self.assertNotIn(sentinel, public_text)
        self.assertEqual(raised.exception.code, 1)
        self.assertRegex(
            public_text,
            r"^CommandError: official_plugin_sync_failed "
            r"correlation_id=[0-9a-f]{32}\r?\n$",
        )
        self.assertNotIn("Traceback", public_text)

    def test_sync_unexpected_failures_use_real_cli_safe_stderr(self):
        sentinels = (
            r"C:\private\official.sqlite token=ORM_TOKEN_CANARY Traceback",
            r"C:\private\publisher.key token=PUBLISH_TOKEN_CANARY Traceback",
        )
        failures = (
            patch(
                "plugin_host.management.commands.sync_official_plugins.get_user_model",
                side_effect=OperationalError(sentinels[0]),
            ),
            patch(
                "plugin_host.management.commands.sync_official_plugins.PluginPackageInstaller.publish",
                side_effect=CommandError(sentinels[1]),
            ),
        )
        for failure, sentinel in zip(failures, sentinels, strict=True):
            errors = StringIO()
            with (
                self.subTest(sentinel=sentinel),
                failure,
                redirect_stderr(errors),
                self.assertRaises(SystemExit) as raised,
            ):
                ManagementUtility(
                    ["manage.py", "sync_official_plugins", "--no-color"]
                ).execute()

            public_text = errors.getvalue()
            self.assertEqual(raised.exception.code, 1)
            self.assertRegex(
                public_text,
                r"^CommandError: official_plugin_sync_failed "
                r"correlation_id=[0-9a-f]{32}\r?\n$",
            )
            self.assertNotIn(sentinel, public_text)
            self.assertNotIn("Traceback", public_text)
