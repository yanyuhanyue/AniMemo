import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from journal.models import AdminAuditLog
from plugin_host.models import (
    PluginData,
    PluginDeployment,
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
