import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0002_external_media_identity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserExternalAccountConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=50)),
                ("auth_method", models.CharField(choices=[("oauth", "OAuth"), ("personal_access_token", "Personal Access Token")], max_length=32)),
                ("external_user_id", models.CharField(max_length=200)),
                ("external_username", models.CharField(max_length=200)),
                ("display_name", models.CharField(blank=True, max_length=200)),
                ("credential_ciphertext", models.TextField()),
                ("credential_key_version", models.CharField(default="v1", max_length=16)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("connected", "已连接"), ("needs_reauthorization", "需要重新授权")], default="connected", max_length=32)),
                ("connected_at", models.DateTimeField()),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_account_connections", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["provider", "id"],
                "indexes": [models.Index(fields=["provider", "status"], name="journal_extacct_status_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("user", "provider"), name="journal_extacct_user_provider_uq"),
                    models.UniqueConstraint(fields=("provider", "external_user_id"), name="journal_extacct_provider_user_uq"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ExternalAccountAuthorizationState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=50)),
                ("state_digest", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_account_authorization_states", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["provider", "expires_at"], name="journal_extauth_expiry_idx")],
            },
        ),
        migrations.CreateModel(
            name="ExternalImportSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=50)),
                ("snapshot", models.JSONField(default=list)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField()),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_import_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["user", "provider", "expires_at"], name="journal_extimport_exp_idx")],
            },
        ),
    ]
