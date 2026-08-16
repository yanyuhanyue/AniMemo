from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from durability.instance import (
    LocalLocatorStore,
    parse_instance_locator,
    publish_instance_locator,
)
from scripts.tests.test_durability_instance import locator_payload
from updater.deployment import HostPaths
from updater.runtime import CanonicalInstanceRegistry


class CanonicalInstanceRegistryTests(unittest.TestCase):
    def test_snapshot_is_the_only_source_of_production_host_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalLocatorStore.testing(Path(directory) / "instance.json")
            locator = parse_instance_locator(locator_payload())
            published = publish_instance_locator(locator, store=store)

            snapshot = CanonicalInstanceRegistry(store=store).snapshot()
            paths = HostPaths.production(snapshot)

            self.assertEqual(snapshot, published)
            self.assertEqual(paths.app_root, Path("/opt/animemo"))
            self.assertEqual(paths.data_root, Path("/data/animemo"))
            self.assertEqual(
                paths.managed_config_path,
                Path("/data/animemo/config/animemo.json"),
            )
            self.assertEqual(paths.listen_host, "127.0.0.1")
            self.assertEqual(paths.listen_port, 8088)
            self.assertEqual(paths.public_origin, "https://animemo.example")


if __name__ == "__main__":
    unittest.main()
