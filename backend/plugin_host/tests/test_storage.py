import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from plugin_host.package import LocalPluginPackageStorage


class PackageStorageTests(SimpleTestCase):
    def test_atomic_store_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory))
            storage.store_package("demo", "1.0.0", b"one")
            storage.store_package("demo", "1.1.0", b"two")
            storage.store_package("demo", "1.2.0", b"three")
            storage.retain_versions("demo", keep=2)
            self.assertEqual(len(list((Path(directory) / "packages" / "demo").glob("*.ajplugin"))), 2)
