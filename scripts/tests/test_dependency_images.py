from __future__ import annotations

import subprocess
import sys
import unittest

from release.contract import (
    POSTGRES_DIGEST,
    POSTGRES_REPOSITORY,
    REDIS_DIGEST,
    REDIS_REPOSITORY,
)
from release.dependency_images import POSTGRES_IMAGE, REDIS_IMAGE, dependency_image


class DependencyImageAuthorityTests(unittest.TestCase):
    def test_contract_reexports_the_single_dependency_image_authority(self):
        self.assertEqual(POSTGRES_IMAGE, f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}")
        self.assertEqual(REDIS_IMAGE, f"{REDIS_REPOSITORY}@{REDIS_DIGEST}")
        self.assertEqual(dependency_image("postgres"), POSTGRES_IMAGE)
        self.assertEqual(dependency_image("redis"), REDIS_IMAGE)

    def test_standalone_cli_needs_no_release_runtime_dependencies(self):
        for role, expected in (("postgres", POSTGRES_IMAGE), ("redis", REDIS_IMAGE)):
            with self.subTest(role=role):
                completed = subprocess.run(
                    [sys.executable, "-I", "release/dependency_images.py", role],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, expected + "\n")


if __name__ == "__main__":
    unittest.main()
