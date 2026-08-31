from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [
        ("plugin_host", "0004_redact_legacy_plugin_diagnostics"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="pluginversion",
            constraint=models.UniqueConstraint(
                models.F("plugin"),
                Lower("version"),
                name="plugin_version_ci_unique",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="pluginversion",
            name="plugin_version_unique",
        ),
    ]
