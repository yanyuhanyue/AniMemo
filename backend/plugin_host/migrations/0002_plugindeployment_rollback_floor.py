from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("plugin_host", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="plugindeployment",
            name="rollback_floor",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
    ]
