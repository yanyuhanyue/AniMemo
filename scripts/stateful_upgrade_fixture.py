#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if not BACKEND.is_dir() and Path("/app/backend").is_dir():
    BACKEND = Path("/app/backend")
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


class FixtureError(RuntimeError):
    pass


def _django_setup():
    import django

    django.setup()


def _migration_applied(app, name):
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    return (app, name) in MigrationRecorder(connection).applied_migrations()


def _assert(condition, detail):
    if not condition:
        raise FixtureError(detail)


def _cas_path(blob):
    from django.conf import settings

    return Path(settings.PLUGIN_ROOT) / blob.storage_path


def _runtime_path(slug, version):
    from django.conf import settings

    return Path(settings.PLUGIN_ROOT) / "runtime" / slug / version


def _verify_package_blob(blob, *, slug, version):
    from plugin_host.package import inspect_package

    path = _cas_path(blob)
    _assert(path.is_file(), f"CAS package file is missing: {blob.sha256}")
    payload = path.read_bytes()
    inspected = inspect_package(payload)
    _assert(len(payload) == blob.size_bytes, f"CAS package size does not match PluginPackageBlob: {blob.sha256}")
    _assert(inspected["sha256"] == blob.sha256, f"CAS package SHA does not match PluginPackageBlob: {blob.sha256}")
    _assert(inspected["manifest"]["slug"] == slug, f"CAS package slug is invalid: {blob.sha256}")
    _assert(inspected["manifest"]["version"] == version, f"CAS package version is invalid: {blob.sha256}")


def seed_state(output_path):
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import transaction
    from journal.models import JournalEntry
    from plugin_host.models import (
        PluginData,
        PluginDeployment,
        PluginProject,
        UserPluginInstallation,
    )
    from plugin_host.runtime import runtime_registry

    output_path = Path(output_path)
    with transaction.atomic():
        actor, _ = get_user_model().objects.get_or_create(
            username="upgrade-gate-admin",
            defaults={"email": "upgrade-gate-admin@example.test", "is_active": True, "is_staff": True, "is_superuser": True},
        )
        actor_updates = []
        for field, value in (("email", "upgrade-gate-admin@example.test"), ("is_active", True), ("is_staff", True), ("is_superuser", True)):
            if getattr(actor, field) != value:
                setattr(actor, field, value)
                actor_updates.append(field)
        if actor_updates:
            actor.save(update_fields=actor_updates)

        user, _ = get_user_model().objects.get_or_create(
            username="upgrade-gate-user-a",
            defaults={"email": "upgrade-gate-user-a@example.test", "is_active": True, "is_staff": False, "is_superuser": False},
        )
        updates = []
        for field, value in (("email", "upgrade-gate-user-a@example.test"), ("is_active", True), ("is_staff", False), ("is_superuser", False)):
            if getattr(user, field) != value:
                setattr(user, field, value)
                updates.append(field)
        if updates:
            user.save(update_fields=updates)

    call_command("sync_official_plugins", verbosity=1)

    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(username="upgrade-gate-user-a")
        journal, _ = JournalEntry.objects.get_or_create(
            user=user,
            title="Stateful Upgrade Gate 记录 A",
            defaults={
                "japanese_title": "ステートフルアップグレードゲート",
                "watch_status": JournalEntry.WatchStatus.COMPLETED,
                "review": "persistent-state-fixture-v1",
                "tags": ["upgrade-gate", "persistent"],
            },
        )
        project = PluginProject.objects.get(slug="watch-history-importer")
        installation, _ = UserPluginInstallation.objects.get_or_create(
            user=user,
            plugin=project,
            defaults={"enabled": True, "config": {"fixture": "stateful-upgrade-v1"}},
        )
        if not installation.enabled or installation.config != {"fixture": "stateful-upgrade-v1"}:
            installation.enabled = True
            installation.config = {"fixture": "stateful-upgrade-v1"}
            installation.save(update_fields=["enabled", "config", "updated_at"])
        plugin_data, _ = PluginData.objects.update_or_create(
            plugin=project,
            user=user,
            namespace="watch_history",
            key="upgrade-gate-record-a",
            defaults={"value": {"fixture": "stateful-upgrade-v1", "episodes": [1, 2, 3]}},
        )
        deployment = PluginDeployment.objects.select_related("current_version__package_blob").get(plugin=project)

    runtime_version = runtime_registry.assert_invariant(project.slug)
    blob = deployment.current_version.package_blob
    _verify_package_blob(blob, slug=project.slug, version=deployment.current_version.version)
    _assert(_runtime_path(project.slug, runtime_version).is_dir(), "Base official plugin runtime is missing.")

    fixture = {
        "schema": 1,
        "user_id": user.pk,
        "username": user.username,
        "journal_id": journal.pk,
        "journal_review": journal.review,
        "installation_id": installation.pk,
        "installation_config": installation.config,
        "plugin_data_id": plugin_data.pk,
        "plugin_data_value": plugin_data.value,
        "plugin_slug": project.slug,
        "plugin_project_id": project.pk,
        "base_plugin_version_id": deployment.current_version_id,
        "base_plugin_version": deployment.current_version.version,
        "base_package_blob_id": blob.pk,
        "base_package_sha": blob.sha256,
        "base_deployment_id": deployment.pk,
        "base_integrations_0001_applied": _migration_applied("integrations", "0001_initial"),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(fixture, ensure_ascii=False, sort_keys=True))
    print("Stateful upgrade base seed: PASS")
    return fixture


def verify_state(input_path):
    from django.contrib.auth import get_user_model
    from journal.models import JournalEntry
    from plugin_host.models import (
        PluginData,
        PluginDeployment,
        PluginPackageBlob,
        PluginProject,
        PluginVersion,
        UserPluginInstallation,
    )
    from plugin_host.runtime import runtime_registry

    fixture = json.loads(Path(input_path).read_text(encoding="utf-8"))
    _assert(fixture.get("schema") == 1, "Unsupported stateful upgrade fixture schema.")

    user = get_user_model().objects.filter(pk=fixture["user_id"], username=fixture["username"]).first()
    _assert(user is not None, "Seed user did not survive the upgrade.")
    journal = JournalEntry.objects.filter(pk=fixture["journal_id"], user=user).first()
    _assert(journal is not None and journal.review == fixture["journal_review"], "Seed JournalEntry changed or is missing.")

    external_identity_status = "NOT_APPLICABLE"
    if _migration_applied("journal", "0002_external_media_identity"):
        from journal.models import ExternalMediaIdentity

        identity_metadata = {
            "title": "Stateful Upgrade Gate",
            "japanese_title": "ステートフルアップグレードゲート",
            "summary": "stateful-upgrade-external-identity-v1",
            "episodes": 12,
            "air_date": "2026-08-09",
            "studio": "AniMemo CI",
            "tags": ["upgrade-gate"],
            "score": 8.8,
            "poster_url": "",
            "thumbnail_url": "",
            "provider_name": "Bangumi",
            "provider_url": "https://bgm.tv/subject/475000",
            "external_id": "475000",
        }
        identity = ExternalMediaIdentity.objects.filter(
            entry=journal,
            provider="bangumi",
            external_id="475000",
        ).first()
        if "external_identity_id" not in fixture:
            _assert(identity is None, "External identity unexpectedly existed before the current-schema verification.")
            identity = ExternalMediaIdentity.objects.create(
                entry=journal,
                provider="bangumi",
                external_id="475000",
                canonical_url="https://bgm.tv/subject/475000",
                metadata=identity_metadata,
            )
            fixture["external_identity_id"] = identity.pk
            fixture["external_identity_metadata"] = identity_metadata
            Path(input_path).write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            external_identity_status = "CREATED"
        else:
            identity = ExternalMediaIdentity.objects.filter(
                pk=fixture["external_identity_id"],
                entry=journal,
                provider="bangumi",
                external_id="475000",
            ).first()
            _assert(identity is not None, "ExternalMediaIdentity did not survive the restart.")
            _assert(identity.canonical_url == "https://bgm.tv/subject/475000", "ExternalMediaIdentity canonical URL changed.")
            _assert(identity.metadata == fixture["external_identity_metadata"], "ExternalMediaIdentity metadata changed.")
            _assert(identity.created_at is not None and identity.updated_at is not None, "ExternalMediaIdentity timestamps are missing.")
            _assert(journal.external_identities.count() == 1, "ExternalMediaIdentity was duplicated after restart.")
            external_identity_status = "PERSISTED"

    external_account_status = "NOT_APPLICABLE"
    credential_encryption_status = "NOT_APPLICABLE"
    if _migration_applied("journal", "0003_external_account_connections"):
        from django.utils import timezone
        from journal.external_accounts.credentials import (
            decrypt_credentials,
            encrypt_credentials,
        )
        from journal.models import UserExternalAccountConnection

        fake_token = "stateful-upgrade-fake-bangumi-token"
        connection = UserExternalAccountConnection.objects.filter(user=user, provider="bangumi").first()
        if "external_account_connection_id" not in fixture:
            _assert(connection is None, "External account connection unexpectedly existed before current-schema verification.")
            ciphertext = encrypt_credentials({"access_token": fake_token, "token_type": "Bearer"})
            _assert(fake_token not in ciphertext, "External account credential was stored in plaintext.")
            connection = UserExternalAccountConnection.objects.create(
                user=user,
                provider="bangumi",
                auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
                external_user_id="987654321",
                external_username="stateful-upgrade-user",
                display_name="Stateful Upgrade User",
                credential_ciphertext=ciphertext,
                credential_key_version="v1",
                metadata={"fixture": "stateful-upgrade-account-v1"},
                connected_at=timezone.now(),
                verified_at=timezone.now(),
            )
            fixture["external_account_connection_id"] = connection.pk
            fixture["external_account_ciphertext"] = ciphertext
            Path(input_path).write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            external_account_status = "CREATED"
        else:
            connection = UserExternalAccountConnection.objects.filter(
                pk=fixture["external_account_connection_id"],
                user=user,
                provider="bangumi",
                external_user_id="987654321",
            ).first()
            _assert(connection is not None, "UserExternalAccountConnection did not survive the restart.")
            _assert(connection.credential_ciphertext == fixture["external_account_ciphertext"], "Encrypted credential changed across restart.")
            external_account_status = "PERSISTED"
        _assert(fake_token not in connection.credential_ciphertext, "Database credential value contains plaintext token.")
        _assert(decrypt_credentials(connection.credential_ciphertext)["access_token"] == fake_token, "Encrypted credential cannot be decrypted after restart.")
        credential_encryption_status = "PASS"

    projects = PluginProject.objects.filter(slug=fixture["plugin_slug"])
    _assert(projects.count() == 1, "Official PluginProject is missing or duplicated.")
    project = projects.get()
    _assert(project.pk == fixture["plugin_project_id"], "Official PluginProject identity changed.")
    _assert(project.status == PluginProject.Status.ACTIVE, "Official PluginProject is not active.")

    installation = UserPluginInstallation.objects.filter(pk=fixture["installation_id"], user=user, plugin=project).first()
    _assert(installation is not None and installation.enabled, "UserPluginInstallation is missing or disabled.")
    _assert(installation.config == fixture["installation_config"], "UserPluginInstallation config changed.")
    plugin_data = PluginData.objects.filter(
        pk=fixture["plugin_data_id"], user=user, plugin=project, namespace="watch_history", key="upgrade-gate-record-a"
    ).first()
    _assert(plugin_data is not None and plugin_data.value == fixture["plugin_data_value"], "PluginData changed or is missing.")

    base_version = PluginVersion.objects.select_related("package_blob").filter(
        pk=fixture["base_plugin_version_id"], plugin=project, version=fixture["base_plugin_version"]
    ).first()
    _assert(base_version is not None, "Base immutable PluginVersion was removed.")
    _assert(base_version.package_blob.sha256 == fixture["base_package_sha"], "Base immutable PluginVersion package SHA changed.")
    _assert(PluginPackageBlob.objects.filter(pk=fixture["base_package_blob_id"], sha256=fixture["base_package_sha"]).exists(), "Base PluginPackageBlob is missing.")
    _verify_package_blob(base_version.package_blob, slug=project.slug, version=base_version.version)
    _assert(_runtime_path(project.slug, base_version.version).is_dir(), "Base runtime directory is missing.")

    deployment = PluginDeployment.objects.select_related("current_version__package_blob").filter(
        pk=fixture["base_deployment_id"], plugin=project
    ).first()
    _assert(deployment is not None, "PluginDeployment is missing.")
    _assert(deployment.enabled and deployment.healthy, "PluginDeployment is not enabled and healthy.")
    _assert(deployment.status == PluginDeployment.Status.ENABLED, "PluginDeployment status is not enabled.")
    _assert(deployment.current_version.review_status == PluginVersion.ReviewStatus.APPROVED, "Current official PluginVersion is not approved.")
    _assert(deployment.current_version.published_at is not None, "Current official PluginVersion is not published.")
    _verify_package_blob(
        deployment.current_version.package_blob,
        slug=project.slug,
        version=deployment.current_version.version,
    )
    _assert(_runtime_path(project.slug, deployment.current_version.version).is_dir(), "Current runtime directory is missing.")

    reconcile_errors = runtime_registry.reconcile_all()
    _assert(not reconcile_errors, f"Runtime reconcile failed: {reconcile_errors}")
    runtime_version = runtime_registry.assert_invariant(project.slug)
    _assert(runtime_version == deployment.current_version.version, "Runtime does not match PluginDeployment.current_version.")

    integrations_applied = _migration_applied("integrations", "0001_initial")
    if not fixture["base_integrations_0001_applied"]:
        _assert(integrations_applied, "Current release did not apply integrations.0001_initial over the existing database.")

    report = {
        "user": "PASS",
        "journal_entry": "PASS",
        "external_media_identity": external_identity_status,
        "external_account_connection": external_account_status,
        "credential_encryption": credential_encryption_status,
        "user_plugin_installation": "PASS",
        "plugin_data": "PASS",
        "plugin_project": "PASS",
        "plugin_version": "PASS",
        "plugin_package_blob": "PASS",
        "plugin_deployment": "PASS",
        "cas": "PASS",
        "runtime": runtime_version,
        "base_integrations_0001_applied": fixture["base_integrations_0001_applied"],
        "current_integrations_0001_applied": integrations_applied,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    print("Stateful upgrade persistence verification: PASS")
    return report


def main():
    parser = argparse.ArgumentParser(description="Seed and verify persistent state for the Docker stateful upgrade gate.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed")
    seed.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()

    _django_setup()
    try:
        if args.command == "seed":
            seed_state(args.output)
        else:
            verify_state(args.input)
    except (FixtureError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Stateful upgrade fixture failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
