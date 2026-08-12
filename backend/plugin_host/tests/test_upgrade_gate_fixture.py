import sys
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from plugin_host.models import PluginData
from plugin_host.runtime import runtime_registry


SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stateful_upgrade_fixture import FixtureError, _bundled_plugin_release, seed_state, verify_state


class StatefulUpgradeFixtureTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        private_root = Path(self.temporary.name) / "private"
        private_root.mkdir(mode=0o700)
        self.setup_code_path = private_root / "setup-code"
        self.setup_code_path.write_text("stale-base-setup-code\n", encoding="utf-8")
        self.setup_code_path.chmod(0o600)
        self.settings = override_settings(
            PLUGIN_ROOT=Path(self.temporary.name) / "plugins",
            PLUGIN_MIN_FREE_DISK_MB=0,
            FIRST_RUN_SETUP_CODE_PATH=self.setup_code_path,
        )
        self.settings.enable()
        self.fixture_path = Path(self.temporary.name) / "fixture.json"

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.temporary.cleanup()

    def test_seed_and_verify_representative_persistent_state(self):
        fixture = seed_state(self.fixture_path)

        first_report = verify_state(self.fixture_path)
        report = verify_state(self.fixture_path)

        self.assertEqual(report["runtime"], fixture["base_plugin_version"])
        self.assertEqual(first_report["external_media_identity"], "PERSISTED")
        self.assertEqual(report["external_media_identity"], "PERSISTED")
        self.assertEqual(first_report["external_account_connection"], "PERSISTED")
        self.assertEqual(report["external_account_connection"], "PERSISTED")
        self.assertEqual(report["credential_encryption"], "PASS")
        self.assertFalse(get_user_model().objects.get(pk=fixture["user_id"]).is_staff)
        self.assertTrue(self.fixture_path.is_file())
        self.assertFalse(self.setup_code_path.exists())

    def test_verify_rejects_changed_plugin_data(self):
        fixture = seed_state(self.fixture_path)
        PluginData.objects.filter(pk=fixture["plugin_data_id"]).update(value={"fixture": "corrupted"})

        with self.assertRaisesRegex(FixtureError, "PluginData"):
            verify_state(self.fixture_path)

    def test_bundled_release_uses_the_current_container_manifest(self):
        base_dir = Path(self.temporary.name) / "base" / "backend"
        manifest_dir = base_dir.parent / "plugins" / "watch-history-importer"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(
            '{"slug":"watch-history-importer","version":"0.4.0","dataCompatibility":{"rollbackFloor":"0.4.0"}}',
            encoding="utf-8",
        )

        with override_settings(BASE_DIR=base_dir):
            self.assertEqual(_bundled_plugin_release("watch-history-importer"), ("0.4.0", "0.4.0"))
