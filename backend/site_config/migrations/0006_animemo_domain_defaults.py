from django.db import migrations


LEGACY_HOST_REPLACEMENTS = {
    "img.re-anime.cc": "media.animemo.cc",
    "media.re-anime.cc": "media.animemo.cc",
    "re-anime.cc": "media.animemo.cc",
}


def migrate_legacy_trusted_poster_hosts(apps, _schema_editor):
    SiteSettings = apps.get_model("site", "SiteSettings")
    for settings_obj in SiteSettings.objects.only("pk", "trusted_poster_hosts").iterator():
        hosts = settings_obj.trusted_poster_hosts
        if not isinstance(hosts, list):
            continue

        migrated = []
        for value in hosts:
            host = str(value or "").strip().lower().rstrip(".")
            host = LEGACY_HOST_REPLACEMENTS.get(host, host)
            if host and host not in migrated:
                migrated.append(host)
        if migrated != hosts:
            SiteSettings.objects.filter(pk=settings_obj.pk).update(
                trusted_poster_hosts=migrated
            )


class Migration(migrations.Migration):
    dependencies = [
        ("site", "0005_animemo_identity_defaults"),
    ]

    operations = [
        migrations.RunPython(
            migrate_legacy_trusted_poster_hosts,
            migrations.RunPython.noop,
        ),
    ]
