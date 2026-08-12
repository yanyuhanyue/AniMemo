import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def seed_installation_state(apps, _schema_editor):
    InstallationState = apps.get_model("site", "InstallationState")
    User = apps.get_model("accounts", "User")
    has_existing_accounts = User.objects.exists()
    InstallationState.objects.update_or_create(
        pk=1,
        defaults={
            "status": "initialized" if has_existing_accounts else "uninitialized",
            "initialized_at": timezone.now() if has_existing_accounts else None,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("site", "0002_media_write_reservation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InstallationState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("uninitialized", "未初始化"),
                            ("initializing", "初始化中"),
                            ("initialized", "已初始化"),
                        ],
                        default="uninitialized",
                        max_length=16,
                    ),
                ),
                ("setup_code_hash", models.CharField(blank=True, default="", editable=False, max_length=256)),
                ("setup_code_issued_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("setup_code_expires_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0, editable=False)),
                ("initialized_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "initialized_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="initialized_installations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"verbose_name": "安装状态", "verbose_name_plural": "安装状态"},
        ),
        migrations.RunPython(seed_installation_state, migrations.RunPython.noop),
    ]
