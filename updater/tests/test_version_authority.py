from __future__ import annotations

import ast
import inspect
import json
import unittest
from pathlib import Path

from release import candidate, cli, mirror
from scripts import generate_distribution_pipeline_evidence, release_notes_snapshot
from updater import __version__
from updater.agent import _BoundReleaseResolver
from updater.local_bundle import LocalBundleReleaseSource
from updater.source import GitHubReleaseSource

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class UpdaterVersionAuthorityTests(unittest.TestCase):
    def test_all_updater_version_defaults_derive_from_package_authority(self):
        consumers = (
            _BoundReleaseResolver.fetch_verified_materials,
            _BoundReleaseResolver.fetch_verified,
            GitHubReleaseSource.fetch_verified_materials,
            GitHubReleaseSource.fetch_verified,
            LocalBundleReleaseSource.fetch_verified_materials,
            LocalBundleReleaseSource.fetch_verified,
        )
        for consumer in consumers:
            with self.subTest(consumer=consumer.__qualname__):
                parameter = inspect.signature(consumer).parameters["updater_version"]
                self.assertEqual(parameter.default, __version__)

    def test_production_python_has_no_literal_updater_version_argument(self):
        findings = []
        for top_level in ("updater", "installer", "release", "scripts"):
            for path in (REPOSITORY_ROOT / top_level).rglob("*.py"):
                relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                if "/tests/" in f"/{relative}" or path.name.startswith("test_"):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        positional = [*node.args.posonlyargs, *node.args.args]
                        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(
                            node.args.defaults
                        )
                        pairs = zip(positional, defaults)
                        keyword_pairs = zip(node.args.kwonlyargs, node.args.kw_defaults)
                        for argument, default in (*pairs, *keyword_pairs):
                            if (
                                argument.arg == "updater_version"
                                and isinstance(default, ast.Constant)
                                and isinstance(default.value, str)
                            ):
                                findings.append((relative, node.lineno))
                    elif isinstance(node, ast.Call):
                        for keyword in node.keywords:
                            if (
                                keyword.arg == "updater_version"
                                and isinstance(keyword.value, ast.Constant)
                                and isinstance(keyword.value.value, str)
                            ):
                                findings.append((relative, node.lineno))
        self.assertEqual(findings, [])

    def test_shell_installer_reads_the_same_parseable_package_authority(self):
        source = (REPOSITORY_ROOT / "updater" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'__version__ = "{__version__}"', source)
        installer = (REPOSITORY_ROOT / "deploy" / "install-updater.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("updater/__init__.py", installer)

    def test_release_tools_use_canonical_updater_and_compatibility_authorities(self):
        compatibility = json.loads(
            (REPOSITORY_ROOT / "release" / "compatibility.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(candidate.updater_version, __version__)
        self.assertEqual(mirror.updater_version, __version__)
        self.assertEqual(
            generate_distribution_pipeline_evidence.updater_version,
            __version__,
        )
        self.assertEqual(
            release_notes_snapshot._minimum_updater_version(),
            compatibility["minimumUpdaterVersion"],
        )
        parser = cli._parser()
        args = parser.parse_args(
            ["validate-manifest", "--manifest", "release-manifest.json"]
        )
        self.assertEqual(args.updater_version, __version__)

    def test_workflows_do_not_override_the_updater_version_authority(self):
        findings = []
        for path in (REPOSITORY_ROOT / ".github" / "workflows").glob("*.y*ml"):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if "--updater-version" in line:
                    findings.append((path.name, line_number))
        self.assertEqual(findings, [])

    def test_release_notes_cli_cannot_override_compatibility_authority(self):
        option_strings = {
            option
            for action in release_notes_snapshot._parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--minimum-updater-version", option_strings)


if __name__ == "__main__":
    unittest.main()
