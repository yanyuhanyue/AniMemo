from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.dr_recovery_paths import (
    PathSafetyError,
    canonical_existing_directory,
    prepare_temp_root,
    validate_delete_target,
)


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


class DisasterRecoveryPathSafetyTests(unittest.TestCase):
    def test_prepare_and_validate_accept_only_a_canonical_direct_child(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            target = prepare_temp_root(parent, parent / "animemo-dr-safe")

            self.assertEqual(target, parent / "animemo-dr-safe")
            self.assertEqual(validate_delete_target(target, parent), target)

    def test_prepare_rejects_parent_nested_relative_dotdot_and_existing_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            existing = parent / "existing"
            existing.mkdir()
            cases = (
                parent,
                parent / "nested" / "child",
                Path("relative-root"),
                Path(os.fspath(parent / "missing")) / ".." / "escape",
                existing,
            )
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(PathSafetyError):
                    prepare_temp_root(parent, candidate)

    def test_delete_validation_rejects_parent_nested_external_and_missing_targets(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as external_directory:
            parent = Path(directory).resolve()
            direct = parent / "direct"
            nested = direct / "nested"
            nested.mkdir(parents=True)
            external = Path(external_directory).resolve()
            cases = (parent, nested, external, parent / "missing")
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(PathSafetyError):
                    validate_delete_target(candidate, parent)

    def test_symlink_or_reparse_components_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            parent = Path(directory).resolve()
            outside = Path(outside_directory).resolve()
            link = parent / "linked"
            try:
                link_directory(link, outside)
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("directory links are unavailable")

            with self.assertRaises(PathSafetyError):
                canonical_existing_directory(link, label="linked target")
            with self.assertRaises(PathSafetyError):
                validate_delete_target(link, parent)


if __name__ == "__main__":
    unittest.main()
