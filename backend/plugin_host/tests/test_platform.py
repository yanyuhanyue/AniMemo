import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from plugin_host.models import PluginData, PluginDeployment, PluginProject, PluginSubmission, PluginVersion, UserPluginInstallation
from plugin_host.runtime import runtime_registry
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
