import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from journal.models import AdminAuditLog
from plugin_host.models import (
    PluginData,
    PluginDeployment,
    PluginPackageBlob,
    PluginProject,
    PluginSubmission,
    PluginUploadAttempt,
    PluginVersion,
    UserPluginInstallation,
)
from plugin_host.runtime import runtime_registry
from plugin_host.package import PluginPackageError
from plugin_host.services import PluginWorkflowError, upload_plugin_version
from plugin_host.tests.test_runtime_e2e import make_package


class PluginPlatformApiTests(APITestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name), PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings.enable()
        self.owner = get_user_model().objects.create_user("owner", "owner@example.com", "password-123")
        self.other = get_user_model().objects.create_user("other", "other@example.com", "password-123")
        self.admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "password-123")
        self.client.force_authenticate(self.owner)

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    def _create_and_upload(self):
        response = self.client.post("/api/plugins/my/", {
            "plugin_id": "com.example.market-demo", "slug": "market-demo",
            "name": "Market Demo", "description": "Marketplace test",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        project_id = response.data["id"]
        payload, _ = make_package("market-demo", "1.0.0", runtimes=["frontend"])
        upload = SimpleUploadedFile("market-demo.ajplugin", payload, content_type="application/zip")
        response = self.client.post(f"/api/plugins/my/{project_id}/versions/", {"archive": upload}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        project = PluginProject.objects.get(pk=project_id)
        return project, project.versions.get()

    def _create_project(self, slug="upload-guard", plugin_id=None):
        return PluginProject.objects.create(
            plugin_id=plugin_id or f"com.example.{slug}",
            slug=slug,
            name=slug,
            description="upload guard",
            owner=self.owner,
        )

    def _upload(self, project, payload, name="plugin.ajplugin"):
        return self.client.post(
            f"/api/plugins/my/{project.pk}/versions/",
            {"archive": SimpleUploadedFile(name, payload, content_type="application/zip")},
            format="multipart",
        )

    def _cas_files(self):
        return list((Path(self.root.name) / "packages" / "sha256").rglob("*.ajplugin"))

    def _create_marketplace_fixture(self, count, *, start_index=0):
        published_at = timezone.now()
        for index in range(start_index, start_index + count):
            slug = f"market-{index:03d}"
            project = PluginProject.objects.create(
                plugin_id=f"com.example.{slug}",
                slug=slug,
                name=f"Marketplace {index:03d}",
                description=f"Marketplace fixture {index:03d}",
                owner=self.owner,
            )
            older_blob = PluginPackageBlob.objects.create(
                sha256=f"{index * 2:064x}", size_bytes=1, storage_path=f"fixtures/{slug}-1.0.0"
            )
            current_blob = PluginPackageBlob.objects.create(
                sha256=f"{index * 2 + 1:064x}", size_bytes=1, storage_path=f"fixtures/{slug}-2.0.0"
            )
            older = PluginVersion.objects.create(
                plugin=project,
                version="1.0.0",
                package_blob=older_blob,
                manifest_snapshot={"permissions": [], "dataPolicy": {"retention": "fixture"}},
                runtime_types=["frontend"],
                review_status=PluginVersion.ReviewStatus.APPROVED,
                published_at=published_at - timedelta(days=1),
                created_by=self.owner,
            )
            current = PluginVersion.objects.create(
                plugin=project,
                version="2.0.0",
                package_blob=current_blob,
                manifest_snapshot={
                    "permissions": [{"code": "journal.read", "name": "Read journal"}],
                    "dataPolicy": {"retention": "current"},
                },
                runtime_types=["frontend", "backend"],
                review_status=PluginVersion.ReviewStatus.APPROVED,
                published_at=published_at,
                created_by=self.owner,
            )
            PluginDeployment.objects.create(
                plugin=project,
                current_version=current,
                enabled=True,
                healthy=True,
                status=PluginDeployment.Status.ENABLED,
            )
            UserPluginInstallation.objects.create(
                user=self.owner,
                plugin=project,
                enabled=index % 2 == 0,
                config={"index": index},
            )
            if index % 2 == 0:
                UserPluginInstallation.objects.create(
                    user=self.other,
                    plugin=project,
                    enabled=True,
                )

            self.assertEqual(older.plugin_id, current.plugin_id)

    def test_marketplace_api_batches_per_plugin_reads_and_preserves_payload_semantics(self):
        self._create_marketplace_fixture(5)
        with CaptureQueriesContext(connection) as small_queries:
            small_response = self.client.get("/api/plugins/marketplace/")

        self.assertEqual(small_response.status_code, 200, small_response.data)
        small_payload = small_response.data["plugins"]
        self.assertEqual([item["slug"] for item in small_payload], [f"market-{i:03d}" for i in range(5)])
        self.assertEqual(small_payload[0]["published_version"], "2.0.0")
        self.assertEqual([item["version"] for item in small_payload[0]["versions"]], ["2.0.0", "1.0.0"])
        self.assertEqual(small_payload[0]["runtime_types"], ["frontend", "backend"])
        self.assertEqual(small_payload[0]["permissions"], [{"code": "journal.read", "name": "Read journal"}])
        self.assertEqual(small_payload[0]["dataPolicy"], {"retention": "current"})
        self.assertEqual(small_payload[0]["install_count"], 2)
        self.assertEqual(small_payload[0]["installation"], {"enabled": True, "config": {"index": 0}})
        self.assertEqual(small_payload[1]["install_count"], 1)
        self.assertEqual(small_payload[1]["installation"], {"enabled": False, "config": {"index": 1}})

        self._create_marketplace_fixture(15, start_index=5)
        with CaptureQueriesContext(connection) as medium_queries:
            medium_response = self.client.get("/api/plugins/marketplace/")

        self.assertEqual(medium_response.status_code, 200, medium_response.data)
        medium_payload = medium_response.data["plugins"]
        self.assertEqual(len(medium_payload), 20)
        self.assertEqual([item["slug"] for item in medium_payload], [f"market-{i:03d}" for i in range(20)])
        self.assertEqual(
            len(medium_queries),
            len(small_queries),
            f"marketplace query count grew with fixture size: {len(small_queries)} -> {len(medium_queries)}",
        )

        self._create_marketplace_fixture(30, start_index=20)
        with CaptureQueriesContext(connection) as large_queries:
            large_response = self.client.get("/api/plugins/marketplace/")

        self.assertEqual(large_response.status_code, 200, large_response.data)
        self.assertEqual(len(large_response.data["plugins"]), 50)
        self.assertEqual(
            len(large_queries),
            len(small_queries),
            f"marketplace query count grew with fixture size: {len(small_queries)} -> {len(large_queries)}",
        )

    def _create_staff_review_fixture(self, count, *, start_index=0):
        published_at = timezone.now()
        for index in range(start_index, start_index + count):
            slug = f"review-{index:03d}"
            project = PluginProject.objects.create(
                plugin_id=f"com.example.{slug}",
                slug=slug,
                name=f"Review {index:03d}",
                description=f"Review fixture {index:03d}",
                owner=self.owner,
            )
            blobs = [
                PluginPackageBlob.objects.create(
                    sha256=f"{index * 10 + version_index:064x}",
                    size_bytes=1,
                    storage_path=f"fixtures/{slug}-{version_index}.ajplugin",
                )
                for version_index in range(4)
            ]
            previous = PluginVersion.objects.create(
                plugin=project,
                version="1.0.0",
                package_blob=blobs[0],
                manifest_snapshot={},
                runtime_types=["frontend"],
                review_status=PluginVersion.ReviewStatus.APPROVED,
                published_at=published_at - timedelta(days=2),
                created_by=self.owner,
            )
            current = PluginVersion.objects.create(
                plugin=project,
                version="2.0.0",
                package_blob=blobs[1],
                manifest_snapshot={},
                runtime_types=["frontend", "backend"],
                review_status=PluginVersion.ReviewStatus.APPROVED,
                published_at=published_at - timedelta(days=1),
                created_by=self.owner,
            )
            approved = PluginVersion.objects.create(
                plugin=project,
                version="3.0.0",
                package_blob=blobs[2],
                manifest_snapshot={},
                runtime_types=["frontend"],
                review_status=PluginVersion.ReviewStatus.APPROVED,
                created_by=self.owner,
            )
            submitted = PluginVersion.objects.create(
                plugin=project,
                version="4.0.0",
                package_blob=blobs[3],
                manifest_snapshot={},
                runtime_types=["frontend"],
                review_status=PluginVersion.ReviewStatus.SUBMITTED,
                created_by=self.owner,
            )
            PluginSubmission.objects.create(
                plugin_version=previous,
                submitter=self.owner,
                status=PluginSubmission.Status.APPROVED,
                security_report={"source": "previous"},
            )
            PluginSubmission.objects.create(
                plugin_version=current,
                submitter=self.owner,
                status=PluginSubmission.Status.APPROVED,
                security_report={"source": "current"},
            )
            PluginSubmission.objects.create(
                plugin_version=approved,
                submitter=self.owner,
                status=PluginSubmission.Status.APPROVED,
                security_report={"source": "approved"},
            )
            submitted_row = PluginSubmission.objects.create(
                plugin_version=submitted,
                submitter=self.owner,
                security_report={"source": "submitted"},
            )
            PluginDeployment.objects.create(
                plugin=project,
                current_version=current,
                previous_version=previous,
                enabled=True,
                healthy=True,
                status=PluginDeployment.Status.ENABLED,
                disk_bytes=123,
            )
            UserPluginInstallation.objects.create(user=self.owner, plugin=project)
            UserPluginInstallation.objects.create(user=self.other, plugin=project)
            if index == start_index:
                expected = {
                    "project": project,
                    "previous": previous,
                    "current": current,
                    "approved": approved,
                    "submitted": submitted_row,
                }
        return expected

    def test_staff_review_queue_query_count_is_bounded_and_preserves_payload_semantics(self):
        expected = self._create_staff_review_fixture(1)
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)

        with CaptureQueriesContext(connection) as small_queries:
            small_response = admin_client.get("/api/staff/plugins/review/")

        self.assertEqual(small_response.status_code, 200, small_response.data)
        small_payload = small_response.data
        self.assertEqual(small_payload["submissions"], [{
            "id": expected["submitted"].pk,
            "version_id": expected["submitted"].plugin_version_id,
            "project": expected["project"].slug,
            "version": "4.0.0",
            "runtime_types": ["frontend"],
            "submitter": self.owner.get_username(),
            "security_report": {"source": "submitted"},
        }])
        self.assertEqual(small_payload["approved_versions"], [{
            "id": expected["approved"].pk,
            "project": expected["project"].slug,
            "version": "3.0.0",
            "runtime_types": ["frontend"],
            "security_report": {"source": "approved"},
        }])
        self.assertEqual(small_payload["deployments"], [{
            "slug": expected["project"].slug,
            "name": expected["project"].name,
            "version_id": expected["current"].pk,
            "version": "2.0.0",
            "previous_version": "1.0.0",
            "enabled": True,
            "healthy": True,
            "status": PluginDeployment.Status.ENABLED,
            "published": True,
            "revoked": False,
            "install_count": 2,
            "disk_bytes": 123,
            "last_error": "",
        }])
        marketplace_by_version = {item["version"]: item for item in small_payload["marketplace_versions"]}
        self.assertEqual(marketplace_by_version, {
            "1.0.0": {
                "id": expected["previous"].pk,
                "project": expected["project"].slug,
                "name": expected["project"].name,
                "version": "1.0.0",
                "runtime_types": ["frontend"],
                "published_at": marketplace_by_version["1.0.0"]["published_at"],
                "install_count": 2,
                "security_report": {"source": "previous"},
            },
            "2.0.0": {
                "id": expected["current"].pk,
                "project": expected["project"].slug,
                "name": expected["project"].name,
                "version": "2.0.0",
                "runtime_types": ["frontend", "backend"],
                "published_at": marketplace_by_version["2.0.0"]["published_at"],
                "install_count": 2,
                "security_report": {"source": "current"},
            },
        })

        self._create_staff_review_fixture(4, start_index=1)
        with CaptureQueriesContext(connection) as large_queries:
            large_response = admin_client.get("/api/staff/plugins/review/")

        self.assertEqual(large_response.status_code, 200, large_response.data)
        self.assertLessEqual(
            len(large_queries),
            len(small_queries) + 4,
            f"staff review query count grew with plugin scale: {len(small_queries)} -> {len(large_queries)}",
        )
        self.assertEqual(len(large_response.data["submissions"]), 5)
        self.assertEqual(len(large_response.data["approved_versions"]), 5)
        self.assertEqual(len(large_response.data["deployments"]), 5)
        self.assertEqual(len(large_response.data["marketplace_versions"]), 10)

    def test_developer_review_publish_marketplace_and_install_flow(self):
        project, version = self._create_and_upload()
        self.assertFalse(PluginDeployment.objects.filter(plugin=project).exists())

        preview = self.client.post(f"/api/plugins/my/versions/{version.pk}/preview/")
        self.assertEqual(preview.status_code, 200, preview.data)
        preview_session = preview.data["preview"].rsplit("/", 1)[-1]
        preview_metadata = self.client.get(f"/api/plugins/previews/{preview_session}/")
        self.assertEqual(preview_metadata.status_code, 200, preview_metadata.data)
        self.assertIn("/plugin-previews/session/", preview_metadata.data["frontendEntry"])
        other_client = APIClient()
        other_client.force_authenticate(self.other)
        self.assertEqual(other_client.post(f"/api/plugins/my/versions/{version.pk}/preview/").status_code, 404)
        self.assertEqual(other_client.get(f"/api/plugins/previews/{preview_session}/").status_code, 404)

        submitted = self.client.post(f"/api/plugins/my/versions/{version.pk}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 201, submitted.data)
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        review_queue = admin_client.get("/api/staff/plugins/review/")
        self.assertEqual(review_queue.status_code, 200, review_queue.data)
        self.assertEqual([item["id"] for item in review_queue.data["submissions"]], [submitted.data["id"]])
        reviewed = admin_client.post(f"/api/staff/plugins/review/{submitted.data['id']}/", {"approve": True}, format="json")
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        published = admin_client.post(f"/api/staff/plugins/versions/{version.pk}/publish/")
        self.assertEqual(published.status_code, 200, published.data)

        market = self.client.get("/api/plugins/marketplace/")
        self.assertEqual([item["slug"] for item in market.data["plugins"]], ["market-demo"])
        installed = self.client.post("/api/plugins/marketplace/market-demo/install/")
        self.assertEqual(installed.status_code, 201, installed.data)
        metadata = self.client.get("/api/plugins/enabled/")
        self.assertIn("/plugin-assets/session/", metadata.data["plugins"][0]["frontendEntry"])
        asset = self.client.get(metadata.data["plugins"][0]["frontendEntry"])
        self.assertEqual(asset.status_code, 200)
        asset.close()

    def test_uninstall_retains_only_current_users_data_by_default(self):
        project, version = self._create_and_upload()
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        from plugin_host.installer import PluginPackageInstaller
        PluginPackageInstaller().publish(version, actor=self.admin)
        UserPluginInstallation.objects.create(user=self.owner, plugin=project)
        UserPluginInstallation.objects.create(user=self.other, plugin=project)
        PluginData.objects.create(plugin=project, user=self.owner, namespace="test", key="one", value={"v": 1})
        PluginData.objects.create(plugin=project, user=self.other, namespace="test", key="two", value={"v": 2})
        response = self.client.delete("/api/plugins/marketplace/market-demo/installation/", {}, format="json")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(PluginData.objects.filter(plugin=project, user=self.owner).exists())
        self.assertTrue(PluginData.objects.filter(plugin=project, user=self.other).exists())
        self.assertTrue(PluginDeployment.objects.filter(plugin=project).exists())

    def test_user_config_isolation(self):
        project = PluginProject.objects.create(
            plugin_id="com.example.config", slug="config-demo", name="Config", description="test", owner=self.owner,
        )
        first = UserPluginInstallation.objects.create(user=self.owner, plugin=project, config={"name": "A"})
        second = UserPluginInstallation.objects.create(user=self.other, plugin=project, config={"name": "B"})
        self.assertNotEqual(first.config, second.config)

    def test_project_update_archive_delete_and_submission_withdrawal(self):
        project, version = self._create_and_upload()
        detail = self.client.get(f"/api/plugins/my/{project.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        updated = self.client.patch(f"/api/plugins/my/{project.pk}/", {"name": "Updated", "description": "Updated description"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["name"], "Updated")

        submitted = self.client.post(f"/api/plugins/my/versions/{version.pk}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 201, submitted.data)
        withdrawn = self.client.post(f"/api/plugins/my/submissions/{submitted.data['id']}/withdraw/")
        self.assertEqual(withdrawn.status_code, 200, withdrawn.data)
        version.refresh_from_db()
        self.assertEqual(version.review_status, PluginVersion.ReviewStatus.DRAFT)
        self.assertEqual(PluginSubmission.objects.get(pk=submitted.data["id"]).status, PluginSubmission.Status.WITHDRAWN)

        deleted = self.client.delete(f"/api/plugins/my/{project.pk}/")
        self.assertEqual(deleted.status_code, 200, deleted.data)
        self.assertEqual(deleted.data["result"], "deleted")
        self.assertFalse(PluginProject.objects.filter(pk=project.pk).exists())

    def test_unpublish_hides_marketplace_but_preserves_installed_runtime_access(self):
        project, version = self._create_and_upload()
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        from plugin_host.installer import PluginPackageInstaller
        PluginPackageInstaller().publish(version, actor=self.admin)
        UserPluginInstallation.objects.create(user=self.owner, plugin=project)

        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        response = admin_client.post(f"/api/staff/plugins/versions/{version.pk}/unpublish/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.client.get("/api/plugins/marketplace/").data["plugins"], [])
        installed = self.client.get("/api/plugins/installed/")
        self.assertEqual([item["slug"] for item in installed.data["plugins"]], ["market-demo"])
        enabled = self.client.get("/api/plugins/enabled/")
        self.assertEqual([item["slug"] for item in enabled.data["plugins"]], ["market-demo"])

    def test_package_policy_endpoint_uses_server_settings(self):
        with override_settings(PLUGIN_MAX_PACKAGE_BYTES=123456, PLUGIN_MAX_FILES=77):
            response = self.client.get("/api/plugins/policy/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["package"]["max_package_bytes"], 123456)
        self.assertEqual(response.data["package"]["max_files"], 77)

    def test_wrong_slug_upload_does_not_write_cas(self):
        project = self._create_project()
        payload, _ = make_package("different-slug", "1.0.0", runtimes=["frontend"])

        response = self._upload(project, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._cas_files(), [])

    def test_wrong_plugin_id_upload_does_not_write_cas(self):
        project = self._create_project()
        payload, _ = make_package(
            project.slug,
            "1.0.0",
            runtimes=["frontend"],
            plugin_id="com.example.wrong-id",
        )

        response = self._upload(project, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._cas_files(), [])

    def test_wrong_installation_mode_upload_does_not_write_cas(self):
        project = self._create_project()
        payload, _ = make_package(
            project.slug,
            "1.0.0",
            runtimes=["frontend"],
            installation_mode="system",
        )

        response = self._upload(project, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._cas_files(), [])

    def test_inactive_project_upload_does_not_write_cas(self):
        project = self._create_project()
        project.status = PluginProject.Status.SUSPENDED
        project.save(update_fields=["status"])
        payload, _ = make_package(project.slug, "1.0.0", runtimes=["frontend"])

        response = self._upload(project, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._cas_files(), [])

    def test_same_version_different_sha_does_not_leave_second_cas_file(self):
        project = self._create_project()
        first, _ = make_package(project.slug, "1.0.0", runtimes=["frontend"], frontend_source="export default 1;")
        second, _ = make_package(project.slug, "1.0.0", runtimes=["frontend"], frontend_source="export default 2;")
        self.assertEqual(self._upload(project, first).status_code, 201)

        response = self._upload(project, second)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self._cas_files()), 1)

    @override_settings(PLUGIN_UPLOADS_PER_HOUR=2)
    def test_failed_upload_attempts_trigger_rate_limit_before_cas_write(self):
        project = self._create_project()
        invalid, _ = make_package("wrong-rate-slug", "1.0.0", runtimes=["frontend"])
        valid, _ = make_package(project.slug, "1.0.0", runtimes=["frontend"])
        self.assertEqual(self._upload(project, invalid).status_code, 400)
        self.assertEqual(self._upload(project, invalid).status_code, 400)

        response = self._upload(project, valid)

        self.assertEqual(response.status_code, 400)
        self.assertIn("上传过于频繁", str(response.data))
        self.assertEqual(PluginUploadAttempt.objects.filter(user=self.owner).count(), 3)
        self.assertEqual(self._cas_files(), [])

    @override_settings(PLUGIN_UPLOADS_PER_HOUR=0)
    def test_rate_limited_upload_does_not_read_or_scan_package(self):
        project = self._create_project("early-rate-limit")
        payload, _ = make_package(project.slug, "1.0.0", runtimes=["frontend"])

        class UnreadUpload:
            name = "early-rate-limit.ajplugin"
            size = len(payload)

            def read(self):
                raise AssertionError("rate-limited upload must not read the body")

        with patch("plugin_host.services.inspect_package") as inspect, \
                patch("plugin_host.services.static_security_scan") as scan, \
                patch("plugin_host.services.store_package_blob") as store:
            with self.assertRaises(PluginWorkflowError):
                upload_plugin_version(project, UnreadUpload(), actor=self.owner)

        inspect.assert_not_called()
        scan.assert_not_called()
        store.assert_not_called()
        attempt = PluginUploadAttempt.objects.get(user=self.owner)
        self.assertEqual(attempt.outcome, "rate_limited")
        self.assertEqual(self._cas_files(), [])

    def test_declared_oversize_upload_does_not_read_or_inspect_package(self):
        project = self._create_project("early-size-limit")

        class UnreadUpload:
            name = "early-size-limit.ajplugin"
            size = 1025

            def read(self):
                raise AssertionError("oversized upload must not read the body")

        with override_settings(PLUGIN_MAX_PACKAGE_BYTES=1024), \
                patch("plugin_host.services.inspect_package") as inspect, \
                patch("plugin_host.services.store_package_blob") as store:
            with self.assertRaises(PluginPackageError) as raised:
                upload_plugin_version(project, UnreadUpload(), actor=self.owner)

        self.assertIn("超过大小限制", str(raised.exception))
        inspect.assert_not_called()
        store.assert_not_called()
        self.assertEqual(self._cas_files(), [])

    def test_wrong_slug_upload_skips_static_security_scan(self):
        project = self._create_project("cheap-identity")
        payload, _ = make_package("different-slug", "1.0.0", runtimes=["frontend"])

        with patch("plugin_host.services.static_security_scan") as scan:
            response = self._upload(project, payload)

        self.assertEqual(response.status_code, 400)
        scan.assert_not_called()

    def test_staff_invalid_package_returns_400_without_publish(self):
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        response = admin_client.post(
            "/api/staff/plugins/install/",
            {"archive": SimpleUploadedFile("broken.ajplugin", b"not-a-zip", content_type="application/zip")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PluginVersion.objects.count(), 0)
        self.assertEqual(PluginDeployment.objects.count(), 0)
        self.assertEqual(self._cas_files(), [])
        self.assertFalse((Path(self.root.name) / "runtime").exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="plugin.staff_publish_upload").exists())

    @override_settings(PLUGIN_MIN_FREE_DISK_MB=1)
    def test_upload_disk_floor_includes_incoming_bytes(self):
        project = self._create_project()
        payload, _ = make_package(project.slug, "1.0.0", runtimes=["frontend"])
        free = 1024 * 1024 + len(payload) - 1

        with patch("plugin_host.package.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
            response = self._upload(project, payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("存储空间不足", str(response.data))
        self.assertEqual(self._cas_files(), [])

    @override_settings(PLUGIN_DRAFT_LIMIT=1)
    def test_draft_limit_rejection_does_not_write_new_cas_file(self):
        first = self._create_project("draft-one")
        second = self._create_project("draft-two")
        first_payload, _ = make_package(first.slug, "1.0.0", runtimes=["frontend"])
        second_payload, _ = make_package(second.slug, "1.0.0", runtimes=["frontend"])
        self.assertEqual(self._upload(first, first_payload).status_code, 201)

        response = self._upload(second, second_payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("草稿数量已达上限", str(response.data))
        self.assertEqual(len(self._cas_files()), 1)

    def test_anonymous_marketplace_does_not_expose_deployment_or_review_diagnostics(self):
        project, version = self._create_and_upload()
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        from plugin_host.installer import PluginPackageInstaller

        PluginPackageInstaller().publish(version, actor=self.admin)
        deployment = PluginDeployment.objects.get(plugin=project)
        deployment.last_error = "private runtime detail"
        deployment.disk_bytes = 987654
        deployment.system_config = {"secret": "private"}
        deployment.save(update_fields=["last_error", "disk_bytes", "system_config"])
        anonymous = APIClient()

        response = anonymous.get("/api/plugins/marketplace/")

        self.assertEqual(response.status_code, 200)
        payload = response.data["plugins"][0]
        for key in ("deployment", "last_error", "disk_bytes", "previous_version", "system_config", "storage_path", "security_report", "package_sha256"):
            self.assertNotIn(key, payload)
            self.assertNotIn(key, payload.get("versions", [{}])[0])
        self.assertEqual(payload["published_version"], "1.0.0")
        self.assertIn("dataPolicy", payload)
