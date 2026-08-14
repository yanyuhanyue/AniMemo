import importlib

from django.apps import apps
from django.test import TestCase

from site_config.models import SiteSettings


domain_migration = importlib.import_module(
    "site_config.migrations.0006_animemo_domain_defaults"
)


class DomainDefaultsMigrationTests(TestCase):
    def test_legacy_animemo_hosts_migrate_to_canonical_media_host(self):
        settings_obj = SiteSettings.load()
        settings_obj.trusted_poster_hosts = [
            "lain.bgm.tv",
            "img.re-anime.cc",
            "media.re-anime.cc",
            "re-anime.cc",
        ]
        settings_obj.save(update_fields=["trusted_poster_hosts", "updated_at"])

        domain_migration.migrate_legacy_trusted_poster_hosts(apps, None)

        settings_obj.refresh_from_db()
        self.assertEqual(
            settings_obj.trusted_poster_hosts,
            ["lain.bgm.tv", "media.animemo.cc"],
        )
