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

from stateful_upgrade_fixture import FixtureError, seed_state, verify_state


class StatefulUpgradeFixtureTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.temporary.name) / "plugins", PLUGIN_MIN_FREE_DISK_MB=0)
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
        self.assertEqual(first_report["external_media_identity"], "CREATED")
        self.assertEqual(report["external_media_identity"], "PERSISTED")
        self.assertFalse(get_user_model().objects.get(pk=fixture["user_id"]).is_staff)
        self.assertTrue(self.fixture_path.is_file())

    def test_verify_rejects_changed_plugin_data(self):
        fixture = seed_state(self.fixture_path)
        PluginData.objects.filter(pk=fixture["plugin_data_id"]).update(value={"fixture": "corrupted"})

        with self.assertRaisesRegex(FixtureError, "PluginData"):
            verify_state(self.fixture_path)
