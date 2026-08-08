from django.test import SimpleTestCase

from plugin_host.manifest import ManifestError, validate_manifest


class ManifestV2Tests(SimpleTestCase):
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
