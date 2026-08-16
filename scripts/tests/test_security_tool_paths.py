from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from runpy import run_path

from scripts import pluginctl

ROOT = Path(__file__).resolve().parents[2]


class SecurityToolPathTests(unittest.TestCase):
    def test_pluginctl_accepts_only_manifest_kebab_case_slugs(self):
        self.assertEqual(
            pluginctl.validate_plugin_slug("watch-history-importer"),
            "watch-history-importer",
        )
        for slug in (
            "../outside",
            "/absolute",
            r"..\outside",
            "Uppercase",
            "-leading",
            "trailing-",
            "double--dash",
        ):
            with (
                self.subTest(slug=slug),
                self.assertRaisesRegex(SystemExit, "invalid plugin slug"),
            ):
                pluginctl.validate_plugin_slug(slug)

    def test_bridge_packager_rejects_arbitrary_output_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.zip"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "package-astrbot-bridge.py"),
                    "--output",
                    str(outside),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertFalse(outside.exists())

        self.assertEqual(completed.returncode, 2)
        self.assertIn("must equal the exact GitHub runner output path", completed.stderr)

    def test_bridge_packager_accepts_only_the_active_runner_target(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "astrbot_plugin_animemo_bridge-0.1.3.zip"
            environ = {
                **os.environ,
                "GITHUB_ACTIONS": "true",
                "RUNNER_TEMP": directory,
            }

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "package-astrbot-bridge.py"),
                    "--output",
                    str(expected),
                ],
                cwd=ROOT,
                env=environ,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(expected.is_file())

    def test_bridge_packager_canonical_target_tracks_metadata_version(self):
        namespace = run_path(str(ROOT / "scripts" / "package-astrbot-bridge.py"))
        output_target = namespace["canonical_output_target"]
        with tempfile.TemporaryDirectory() as directory:
            output_target.__globals__["OUT"] = Path(directory) / "dist"
            output_target.__globals__["version"] = lambda: "9.8.7"

            target = output_target()

            self.assertEqual(target.name, "astrbot_plugin_animemo_bridge-9.8.7.zip")

    def test_bridge_packager_rejects_a_symlinked_dist_root(self):
        namespace = run_path(str(ROOT / "scripts" / "package-astrbot-bridge.py"))
        output_target = namespace["canonical_output_target"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            linked_dist = root / "dist"
            try:
                linked_dist.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if error.winerror == 1314:
                    self.skipTest("symlink creation is unavailable on this Windows host")
                raise
            output_target.__globals__["OUT"] = linked_dist

            with self.assertRaisesRegex(RuntimeError, "output directory must not be a symbolic link"):
                output_target()

    def test_astrbot_runtime_smoke_ignores_external_root_and_cleans_its_temp_root(self):
        namespace = run_path(str(ROOT / "scripts" / "smoke-astrbot-bridge-runtime.py"))
        isolated_root = namespace["isolated_astrbot_root"]
        with tempfile.TemporaryDirectory() as directory:
            sentinel_root = Path(directory) / "sentinel"
            sentinel_root.mkdir()
            sentinel = sentinel_root / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            environ = {"ASTRBOT_ROOT": str(sentinel_root)}

            with isolated_root(environ) as selected_root:
                self.assertNotEqual(selected_root, sentinel_root.resolve())
                self.assertEqual(environ["ASTRBOT_ROOT"], str(selected_root))
                self.assertTrue(selected_root.is_dir())

            self.assertFalse(selected_root.exists())
            self.assertEqual(environ["ASTRBOT_ROOT"], str(sentinel_root))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
