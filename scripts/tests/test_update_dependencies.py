import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import update_dependencies


class DependencyLockTests(unittest.TestCase):
    def test_missing_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "requirements.in"
            input_path.write_text("Django>=5.2,<5.3\n", encoding="utf-8")
            self.assertEqual(update_dependencies.check(input_path, root / "requirements.txt"), 1)

    def test_drift_fixture_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "requirements.in"
            lock_path = root / "requirements.txt"
            input_path.write_text("Django>=5.2,<5.3\n", encoding="utf-8")
            lock_path.write_text("Django==5.2.16\n", encoding="utf-8")

            def compile_fixture(_input, output, *, upgrade):
                output.write_text("Django==5.2.17\n", encoding="utf-8")

            with patch.object(update_dependencies, "compile_lock", compile_fixture):
                self.assertEqual(update_dependencies.check(input_path, lock_path), 1)

    def test_direct_constraints_accept_pinned_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "requirements.in"
            lock_path = root / "requirements.txt"
            input_path.write_text("Django>=5.2,<5.3\n", encoding="utf-8")
            lock_path.write_text("Django==5.2.17\n", encoding="utf-8")
            self.assertEqual(update_dependencies.validate_direct_constraints(input_path, lock_path), [])

    def test_lock_comparison_ignores_generator_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text(
                "# pip-compile on Windows\nDjango==5.2.17\n    # via -r C:\\repo\\backend\\requirements.in\n",
                encoding="utf-8",
            )
            second.write_text(
                "# pip-compile on Ubuntu\nDjango==5.2.17\n    # via -r /home/runner/work/AniMemo/backend/requirements.in\n",
                encoding="utf-8",
            )
            self.assertEqual(update_dependencies.normalized_lock(first), update_dependencies.normalized_lock(second))

    def test_lock_comparison_accepts_active_platform_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marked = root / "marked.txt"
            unmarked = root / "unmarked.txt"
            marked.write_text('tzdata==2026.3 ; sys_platform == "win32"\n', encoding="utf-8")
            unmarked.write_text("tzdata==2026.3\n", encoding="utf-8")
            self.assertEqual(update_dependencies.normalized_lock(marked), update_dependencies.normalized_lock(unmarked))
