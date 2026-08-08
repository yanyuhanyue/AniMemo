import hashlib
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from plugin_host.package import LocalPluginPackageStorage


class PackageStorageTests(SimpleTestCase):
    def test_cas_deduplicates_and_keeps_referenced_runtime_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory))
            first = storage.store_package(b"same")
            second = storage.store_package(b"same")
            self.assertEqual(first, second)
            digest = hashlib.sha256(b"same").hexdigest()
            self.assertEqual(first, Path(directory) / "packages" / "sha256" / digest[:2] / f"{digest}.ajplugin")
            runtime = Path(directory) / "runtime" / "demo"
            (runtime / "1.0.0").mkdir(parents=True)
            (runtime / "1.1.0").mkdir()
            storage.retain_versions("demo", current="1.1.0", previous="1.0.0", keep=2)
            self.assertTrue((runtime / "1.0.0").is_dir())
            self.assertTrue((runtime / "1.1.0").is_dir())
