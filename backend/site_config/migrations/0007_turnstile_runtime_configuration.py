from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("site", "0006_animemo_domain_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="turnstile_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="turnstile_site_key",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="turnstile_secret_encrypted",
            field=models.TextField(blank=True, default="", editable=False),
        ),
    ]
