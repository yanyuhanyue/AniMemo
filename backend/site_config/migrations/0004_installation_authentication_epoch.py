import secrets

from django.db import migrations, models


def seed_authentication_epoch(apps, _schema_editor):
    InstallationState = apps.get_model("site", "InstallationState")
    installation = InstallationState.objects.filter(pk=1).first()
    if (
        installation is not None
        and installation.status == "initialized"
        and not installation.authentication_epoch
    ):
        installation.authentication_epoch = secrets.token_hex(32)
        installation.save(update_fields=["authentication_epoch"])


class Migration(migrations.Migration):
    dependencies = [
        ("site", "0003_installation_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="installationstate",
            name="authentication_epoch",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                max_length=64,
            ),
        ),
        migrations.RunPython(
            seed_authentication_epoch,
            migrations.RunPython.noop,
        ),
    ]
