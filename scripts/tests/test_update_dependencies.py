import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.markers import default_environment

from scripts import update_dependencies


class DependencyLockTests(unittest.TestCase):
    def test_cli_rejects_hidden_arbitrary_dependency_paths(self):
        with (
            patch(
                "sys.argv",
                [
                    "update_dependencies.py",
                    "--check",
                    "--input",
                    "../outside.in",
                    "--lock",
                    "../outside.txt",
                ],
            ),
            patch.object(update_dependencies, "check") as check,
            self.assertRaises(SystemExit),
        ):
            update_dependencies.main()
        check.assert_not_called()

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
            marked.write_text('tzdata==2026.3 ; python_version >= "3"\n', encoding="utf-8")
            unmarked.write_text("tzdata==2026.3\n", encoding="utf-8")
            self.assertEqual(update_dependencies.normalized_lock(marked), update_dependencies.normalized_lock(unmarked))

    def test_platform_markers_are_filtered_before_pin_deduplication(self):
        lines = ['demo==1 ; sys_platform == "linux"\n', 'demo==2 ; sys_platform == "win32"\n']
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / 'requirements.txt'
            for platform, version in (('linux', '1'), ('win32', '2')):
                environment = {**default_environment(), 'sys_platform': platform}
                for ordered in (lines, list(reversed(lines))):
                    with self.subTest(platform=platform, ordered=ordered), \
                            patch('packaging.markers.default_environment', return_value=environment):
                        locked.write_text(''.join(ordered), encoding='utf-8')
                        self.assertEqual(update_dependencies.locked_versions(locked), {'demo': version})
                        self.assertEqual(update_dependencies.normalized_lock(locked), f'demo demo=={version}\n')

    def test_direct_constraints_ignore_only_inactive_platform_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            source, locked = Path(directory) / 'requirements.in', Path(directory) / 'requirements.txt'
            source.write_text('missing==1 ; sys_platform == "win32"\n', encoding='utf-8')
            locked.write_text('other==1\n', encoding='utf-8')
            for platform in ('linux', 'win32'):
                with self.subTest(platform=platform), patch('packaging.markers.default_environment',
                        return_value={**default_environment(), 'sys_platform': platform}):
                    self.assertEqual(bool(update_dependencies.validate_direct_constraints(source, locked)),
                                     platform == 'win32')

    def test_conflicting_active_pins_fail_before_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            source, locked = Path(directory) / 'requirements.in', Path(directory) / 'requirements.txt'
            source.write_text('demo-pkg>=1\n', encoding='utf-8')
            for platform in ('linux', 'win32'):
                environment = {**default_environment(), 'sys_platform': platform}
                lines = ['Demo_Pkg==1\n', f'demo.pkg==2 ; sys_platform == "{platform}"\n']
                for ordered in (lines, list(reversed(lines))):
                    with self.subTest(platform=platform, ordered=ordered), \
                            patch('packaging.markers.default_environment', return_value=environment), \
                            patch.object(update_dependencies, 'compile_lock') as compile_lock:
                        locked.write_text(''.join(ordered), encoding='utf-8')
                        with self.assertRaisesRegex(ValueError, 'conflicting active'):
                            update_dependencies.normalized_lock(locked)
                        self.assertEqual(update_dependencies.check(source, locked), 1)
                        compile_lock.assert_not_called()

    def test_equivalent_active_pins_and_normalized_names_remain_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            source, locked = Path(directory) / 'requirements.in', Path(directory) / 'requirements.txt'
            source.write_text('demo.pkg>=1\n', encoding='utf-8')
            locked.write_text('Demo_Pkg==1\ndemo-pkg==1 ; python_version >= "3"\n', encoding='utf-8')
            self.assertEqual(update_dependencies.validate_direct_constraints(source, locked), [])
            self.assertEqual(update_dependencies.normalized_lock(locked), 'demo-pkg demo-pkg==1\n')
