from django.test import SimpleTestCase

from plugin_host.manifest import ManifestError, validate_manifest


class ManifestV2Tests(SimpleTestCase):
    @staticmethod
    def valid_manifest(**updates):
        manifest = {
            "schemaVersion": 2, "sdkApi": 2, "id": "com.example.demo", "slug": "demo",
            "name": "Demo", "version": "1.0.0", "description": "Demo", "license": "MIT",
            "author": {"name": "Example"}, "installationMode": "user",
            "runtimes": [], "extensions": [], "permissions": [], "settings": [],
            "hooks": [], "dataPolicy": {k: False for k in ("storesPersonalData", "usesExternalNetwork", "acceptsFileUploads", "retainsDataOnDisable")},
        }
        manifest.update(updates)
        return manifest

    def test_rejects_v1(self):
        with self.assertRaises(ManifestError):
            validate_manifest({"schemaVersion": 1, "sdkApi": 1})

    def test_rejects_unknown_role(self):
        manifest = {
            "schemaVersion": 2, "sdkApi": 2, "id": "com.example.demo", "slug": "demo",
            "name": "Demo", "version": "1.0.0", "description": "Demo",
            "runtimes": [], "extensions": [], "permissions": [{"code": "demo.run", "roles": ["admin"]}],
            "hooks": [], "dataPolicy": {k: False for k in ("storesPersonalData", "usesExternalNetwork", "acceptsFileUploads", "retainsDataOnDisable")},
        }
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)

    def test_user_plugin_rejects_system_scoped_hook(self):
        with self.assertRaises(ManifestError):
            validate_manifest(self.valid_manifest(hooks=["registration.before_request"]))

    def test_system_plugin_can_declare_global_lifecycle_hook(self):
        manifest = self.valid_manifest(
            installationMode="system",
            hooks=["registration.before_request", "user.after_created"],
        )
        self.assertEqual(validate_manifest(manifest), manifest)

    def test_plugins_without_integrations_remain_valid(self):
        manifest = self.valid_manifest()
        self.assertNotIn("integrations", validate_manifest(manifest))

    def test_data_compatibility_floor_is_optional_and_bounded_by_version(self):
        manifest = self.valid_manifest(
            version="2.0.0",
            dataCompatibility={"rollbackFloor": "1.5.0"},
        )
        self.assertEqual(validate_manifest(manifest)["dataCompatibility"]["rollbackFloor"], "1.5.0")
        with self.assertRaisesRegex(ManifestError, "不能高于当前版本"):
            validate_manifest(self.valid_manifest(
                version="2.0.0",
                dataCompatibility={"rollbackFloor": "3.0.0"},
            ))

    def test_integration_declarations_are_optional_and_provider_neutral(self):
        manifest = self.valid_manifest(
            extensions=["integration.actions", "integration.events"],
            integrations={
                "actions": [{"name": "import-text", "description": "Import text."}],
                "events": [{"name": "import-completed"}],
            },
        )
        self.assertEqual(validate_manifest(manifest), manifest)

    def test_integration_action_requires_capability_extension(self):
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(integrations={"actions": [{"name": "run"}]})
            )

    def test_integration_names_are_conservative_and_unique(self):
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(
                    extensions=["integration.actions"],
                    integrations={"actions": [{"name": "Bad.Name"}]},
                )
            )
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(
                    extensions=["integration.events"],
                    integrations={"events": [{"name": "done"}, {"name": "done"}]},
                )
            )
