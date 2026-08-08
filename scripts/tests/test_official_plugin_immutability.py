import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPTS))

from check_official_plugin_immutability import GateInputError, check_repository


class OfficialPluginImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self._git("init")
        self._git("config", "user.email", "ci@example.test")
        self._git("config", "user.name", "CI")

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def _write_registry(self, slugs):
        source = (ROOT / "backend" / "plugin_host" / "official_packages.py").read_text(encoding="utf-8")
        replacement = f"OFFICIAL_PLUGIN_SLUGS = {tuple(slugs)!r}"
        source = re.sub(r"^OFFICIAL_PLUGIN_SLUGS = .*?$", replacement, source, count=1, flags=re.MULTILINE)
        target = self.repo / "backend" / "plugin_host" / "official_packages.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def _write_plugin(
        self,
        slug,
        version="0.3.0",
        *,
        frontend="front",
        backend="back",
        asset=b"asset",
        description="fixture",
    ):
        root = self.repo / "plugins" / slug
        (root / "frontend").mkdir(parents=True, exist_ok=True)
        (root / "frontend" / "assets").mkdir(parents=True, exist_ok=True)
        (root / "backend").mkdir(parents=True, exist_ok=True)
        manifest = {"id": f"com.example.{slug}", "slug": slug, "version": version, "description": description}
        (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        (root / "frontend" / "plugin.js").write_text(frontend, encoding="utf-8")
        (root / "frontend" / "assets" / "fixture.bin").write_bytes(asset)
        (root / "backend" / "plugin.py").write_text(backend, encoding="utf-8")
        (root / "README.md").write_text("docs", encoding="utf-8")

    def _commit(self, message):
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _base(self, plugins=("alpha",), versions=None):
        versions = versions or {}
        self._write_registry(plugins)
        for slug in plugins:
            self._write_plugin(slug, versions.get(slug, "0.3.0"))
        return self._commit("base")

    def _report(self, base, head):
        return check_repository(self.repo, base, head)

    def test_same_package_same_version_passes(self):
        base = self._base()
        (self.repo / "unrelated.txt").write_text("change", encoding="utf-8")
        head = self._commit("unrelated")
        self.assertTrue(self._report(base, head).ok)

    def test_changed_package_same_version_fails(self):
        base = self._base()
        self._write_plugin("alpha", backend="changed")
        head = self._commit("changed")
        report = self._report(base, head)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].code, "immutable_content_changed")

    def test_same_content_with_different_archive_compression_passes(self):
        base = self._base()
        module = self.repo / "backend" / "plugin_host" / "official_packages.py"
        source = module.read_text(encoding="utf-8").replace("ZIP_DEFLATED", "ZIP_STORED")
        module.write_text(source, encoding="utf-8")
        head = self._commit("compression implementation changed")

        report = self._report(base, head)

        self.assertTrue(report.ok)
        result = report.results[0]
        self.assertNotEqual(result.base.package_sha, result.current.package_sha)
        self.assertEqual(result.base.content_digest, result.current.content_digest)

    def test_changed_package_with_patch_bump_passes(self):
        base = self._base()
        self._write_plugin("alpha", version="0.3.1", backend="changed")
        head = self._commit("patch")
        self.assertTrue(self._report(base, head).ok)

    def test_changed_package_with_minor_bump_passes(self):
        base = self._base()
        self._write_plugin("alpha", version="0.4.0", frontend="changed")
        head = self._commit("minor")
        self.assertTrue(self._report(base, head).ok)

    def test_version_downgrade_fails(self):
        base = self._base(versions={"alpha": "0.4.0"})
        self._write_plugin("alpha", version="0.3.9")
        head = self._commit("downgrade")
        self.assertEqual(self._report(base, head).violations[0].code, "version_downgrade")

    def test_invalid_version_fails(self):
        base = self._base()
        self._write_plugin("alpha", version="version-one")
        head = self._commit("invalid")
        with self.assertRaises(GateInputError):
            self._report(base, head)

    def test_new_official_plugin_passes(self):
        base = self._base()
        self._write_registry(("alpha", "beta"))
        self._write_plugin("beta", version="1.0.0")
        head = self._commit("new")
        report = self._report(base, head)
        self.assertTrue(report.ok)
        self.assertEqual({item.slug: item.status for item in report.results}["beta"], "NEW")

    def test_removed_official_plugin_fails(self):
        base = self._base(("alpha", "beta"))
        self._write_registry(("alpha",))
        shutil.rmtree(self.repo / "plugins" / "beta")
        head = self._commit("removed")
        self.assertEqual(self._report(base, head).violations[0].code, "removed_official_plugin")

    def test_multiple_official_plugins_are_checked_independently(self):
        base = self._base(("alpha", "beta"))
        self._write_plugin("beta", version="0.3.1", frontend="beta-changed")
        head = self._commit("beta bump")
        report = self._report(base, head)
        self.assertTrue(report.ok)
        self.assertEqual([item.slug for item in report.results], ["alpha", "beta"])

    def test_frontend_only_package_change_is_detected(self):
        base = self._base()
        self._write_plugin("alpha", frontend="changed")
        head = self._commit("frontend")
        self.assertFalse(self._report(base, head).ok)

    def test_backend_only_package_change_is_detected(self):
        base = self._base()
        self._write_plugin("alpha", backend="changed")
        head = self._commit("backend")
        self.assertFalse(self._report(base, head).ok)

    def test_asset_only_package_change_is_detected(self):
        base = self._base()
        self._write_plugin("alpha", asset=b"changed")
        head = self._commit("asset")
        self.assertFalse(self._report(base, head).ok)

    def test_manifest_package_change_is_detected(self):
        base = self._base()
        self._write_plugin("alpha", description="changed")
        head = self._commit("manifest")
        self.assertFalse(self._report(base, head).ok)

    def test_package_excluded_documentation_change_passes(self):
        base = self._base()
        (self.repo / "plugins" / "alpha" / "README.md").write_text("changed docs", encoding="utf-8")
        head = self._commit("docs")
        self.assertTrue(self._report(base, head).ok)

    def test_git_attributes_change_does_not_rewrite_blob_identity(self):
        self._git("config", "core.autocrlf", "true")
        base = self._base()
        (self.repo / ".gitattributes").write_text("plugins/**/*.py text eol=lf\n", encoding="utf-8")
        head = self._commit("attributes")

        self.assertTrue(self._report(base, head).ok)
