import hashlib

from django.db import migrations, models


LEGACY_DEFAULT_DIGESTS = {
    "site_name": ("1c998d79df1ddc713048a69a3747c3a2b56673e43907dde2b884e25e53df434d", "AniMemo"),
    "homepage_title": ("643620c12b4eafdd3f91d7482683b19d75c416719149899984981cb22c1c13d3", "AniMemo · 我的动漫记忆库"),
    "homepage_description": (
        "0cfddaf6019ae099764a2f7045569b34b430706b03e11ed219f89521d9250fb8",
        "把想看、在看与看完的作品收进同一条记忆轨迹，随时回望每一次与动画相遇的时刻。",
    ),
    "social_handle": ("074279bedaf90667c24f40ca4b37aa611a4fbad9c1ee947efde71824b97143f3", "X: @ANIMEMO"),
}


def migrate_legacy_defaults(apps, _schema_editor):
    SiteSettings = apps.get_model("site", "SiteSettings")
    for field_name, (legacy_digest, current) in LEGACY_DEFAULT_DIGESTS.items():
        values = SiteSettings.objects.values_list("pk", field_name).iterator()
        for primary_key, value in values:
            digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
            if digest == legacy_digest:
                SiteSettings.objects.filter(pk=primary_key).update(**{field_name: current})


class Migration(migrations.Migration):
    dependencies = [
        ("site", "0004_installation_authentication_epoch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sitesettings",
            name="site_name",
            field=models.CharField(default="AniMemo", max_length=120),
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="homepage_title",
            field=models.CharField(default="AniMemo · 我的动漫记忆库", max_length=160),
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="homepage_description",
            field=models.CharField(
                default="把想看、在看与看完的作品收进同一条记忆轨迹，随时回望每一次与动画相遇的时刻。",
                max_length=320,
            ),
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="social_handle",
            field=models.CharField(default="X: @ANIMEMO", max_length=80),
        ),
        migrations.RunPython(migrate_legacy_defaults, migrations.RunPython.noop),
    ]
