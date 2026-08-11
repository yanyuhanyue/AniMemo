from types import SimpleNamespace

from django.test import SimpleTestCase

from plugin_host.errors import HostCapabilityError
from plugin_host.hooks import HookRegistry
from plugin_host.runtime.context import PluginContext


class PluginContextContractTests(SimpleTestCase):
    @staticmethod
    def manifest(**updates):
        value = {
            "schemaVersion": 2,
            "sdkApi": 2,
            "id": "com.example.contract",
            "slug": "contract",
            "name": "Contract",
            "version": "1.0.0",
            "description": "test",
            "author": {"name": "Example"},
            "license": "MIT",
            "installationMode": "user",
            "runtimes": ["backend"],
            "extensions": ["backend.api"],
            "permissions": [],
            "settings": [],
            "hooks": [],
            "dataPolicy": {
                "storesPersonalData": False,
                "usesExternalNetwork": False,
                "acceptsFileUploads": False,
                "retainsDataOnDisable": True,
            },
        }
        value.update(updates)
        return value

    def test_undeclared_core_capability_is_denied_at_bind_boundary(self):
        context = PluginContext(
            slug="contract",
            version="1.0.0",
            root=SimpleNamespace(),
            manifest=self.manifest(),
            hook_registry=HookRegistry(),
        )
        with self.assertRaises(HostCapabilityError) as caught:
            context.journal.bind(SimpleNamespace())
        self.assertEqual(caught.exception.code, "capability_not_declared")

    def test_storage_and_settings_require_declared_extensions(self):
        context = PluginContext(
            slug="contract",
            version="1.0.0",
            root=SimpleNamespace(),
            manifest=self.manifest(),
            hook_registry=HookRegistry(),
        )
        with self.assertRaises(HostCapabilityError):
            context.storage(user=SimpleNamespace(pk=1), namespace="test")
        with self.assertRaises(HostCapabilityError):
            _ = context.system_settings

    def test_declared_core_capability_is_exposed_without_implicit_storage(self):
        context = PluginContext(
            slug="contract",
            version="1.0.0",
            root=SimpleNamespace(),
            manifest=self.manifest(coreCapabilities=["journal"], extensions=["backend.api", "storage"]),
            hook_registry=HookRegistry(),
        )
        self.assertIn("journal", context.core_capabilities)
        self.assertNotIn("analytics", context.core_capabilities)
