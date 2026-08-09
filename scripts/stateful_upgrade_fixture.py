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
    from django.utils import timezone
    from integrations.models import ExternalIdentityBinding, IntegrationConnection
    from journal.external_accounts.credentials import encrypt_credentials
    from journal.models import (
        ExternalMediaIdentity,
        JournalEntry,
        UserExternalAccountConnection,
    )
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
        watch_history_value = [
            {
                "watched_on": "2026-07-01",
                "watched_label": "2026年7月1日",
                "brush_number": 1,
                "brush_label": "首刷",
                "episode_start": 1,
                "episode_end": 3,
                "notes": ["stateful-upgrade-note-a"],
                "source_document_label": "fixture-document-a",
            },
            {
                "watched_on": "2026-07-08",
                "watched_label": "2026年7月8日",
                "brush_number": 2,
                "brush_label": "二刷",
                "episode_start": 4,
                "episode_end": 6,
                "notes": ["stateful-upgrade-note-b"],
            },
        ]
        core_history_applied = _migration_applied("journal", "0004_core_watch_history_and_metadata_source")
        if core_history_applied:
            from journal.watch_history import replace_history

            PluginData.objects.filter(
                plugin=project,
                user=user,
                namespace="watch_history",
                key=str(journal.pk),
            ).delete()
            replace_history(user=user, entry=journal, records=watch_history_value)
            watch_plugin_data = None
        else:
            watch_plugin_data, _ = PluginData.objects.update_or_create(
                plugin=project,
                user=user,
                namespace="watch_history",
                key=str(journal.pk),
                defaults={"value": watch_history_value},
            )
        plugin_data, _ = PluginData.objects.update_or_create(
            plugin=project,
            user=user,
            namespace="fixture_state",
            key="upgrade-gate-record-a",
            defaults={"value": {"fixture": "stateful-upgrade-v1", "episodes": [1, 2, 3]}},
        )
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
        identity_defaults = {
            "external_id": "475000",
            "canonical_url": "https://bgm.tv/subject/475000",
            "metadata": identity_metadata,
        }
        if core_history_applied:
            identity_defaults.update({"metadata_schema_version": 1, "is_metadata_source": True})
        identity, _ = ExternalMediaIdentity.objects.update_or_create(
            entry=journal,
            provider="bangumi",
            defaults=identity_defaults,
        )
        fake_token = "stateful-upgrade-fake-bangumi-token"
        account_ciphertext = encrypt_credentials({"access_token": fake_token, "token_type": "Bearer"})
        account, _ = UserExternalAccountConnection.objects.update_or_create(
            user=user,
            provider="bangumi",
            defaults={
                "auth_method": UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
                "external_user_id": "987654321",
                "external_username": "stateful-upgrade-user",
                "display_name": "Stateful Upgrade User",
                "credential_ciphertext": account_ciphertext,
                "credential_key_version": "v1",
                "metadata": {"fixture": "stateful-upgrade-account-v1"},
                "connected_at": timezone.now(),
                "verified_at": timezone.now(),
            },
        )
        sync_state = None
        sync_baselines = {"watch_status": {"present": True, "value": "completed"}}
        if _migration_applied("journal", "0005_external_collection_sync_state"):
            from journal.models import ExternalCollectionSyncState

            # This release fixture deliberately seeds pre-upgrade state. Runtime
            # application writes use journal.external_sync.state exclusively.
            sync_state, _ = ExternalCollectionSyncState.objects.update_or_create(
                identity=identity,
                defaults={
                    "connection": account,
                    "schema_version": 1,
                    "baselines": sync_baselines,
                    "last_synced_at": timezone.now(),
                },
            )
        integration, _ = IntegrationConnection.objects.get_or_create(
            provider="astrbot",
            instance_id="stateful-upgrade-instance",
            defaults={
                "name": "Stateful Upgrade AstrBot",
                "key_id": "stateful-upgrade-key",
                "encrypted_secret": "",
            },
        )
        integration.set_secret("stateful-upgrade-integration-secret")
        integration.enabled = True
        integration.save(update_fields=["encrypted_secret", "enabled", "updated_at"])
        binding, _ = ExternalIdentityBinding.objects.update_or_create(
            connection=integration,
            platform="qq",
            external_user_id="stateful-upgrade-external-user",
            defaults={
                "user": user,
                "display_name": "Stateful Upgrade User",
                "enabled": True,
                "verified_at": timezone.now(),
            },
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
        "watch_plugin_data_id": watch_plugin_data.pk if watch_plugin_data is not None else None,
        "watch_plugin_data_value": watch_history_value,
        "external_identity_id": identity.pk,
        "external_identity_metadata": identity.metadata,
        "external_account_connection_id": account.pk,
        "external_account_ciphertext": account.credential_ciphertext,
        "external_sync_state_id": sync_state.pk if sync_state is not None else None,
        "external_sync_baselines": sync_baselines if sync_state is not None else None,
        "external_sync_last_synced_at": sync_state.last_synced_at.isoformat() if sync_state is not None else None,
        "integration_connection_id": str(integration.pk),
        "integration_binding_id": binding.pk,
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

    from integrations.models import ExternalIdentityBinding, IntegrationConnection
    from journal.external_accounts.credentials import decrypt_credentials
    from journal.models import ExternalMediaIdentity, UserExternalAccountConnection

    identity = ExternalMediaIdentity.objects.filter(
        pk=fixture["external_identity_id"],
        entry=journal,
        provider="bangumi",
        external_id="475000",
    ).first()
    _assert(identity is not None, "ExternalMediaIdentity did not survive the upgrade or restart.")
    _assert(identity.canonical_url == "https://bgm.tv/subject/475000", "ExternalMediaIdentity canonical URL changed.")
    _assert(identity.metadata == fixture["external_identity_metadata"], "ExternalMediaIdentity metadata changed.")
    _assert(journal.external_identities.count() == 1, "ExternalMediaIdentity was duplicated.")
    if _migration_applied("journal", "0004_core_watch_history_and_metadata_source"):
        _assert(identity.metadata_schema_version == 1, "ExternalMediaIdentity metadata schema is not v1.")
        _assert(identity.is_metadata_source, "Existing ExternalMediaIdentity was not assigned as metadata source.")

    fake_token = "stateful-upgrade-fake-bangumi-token"
    connection = UserExternalAccountConnection.objects.filter(
        pk=fixture["external_account_connection_id"],
        user=user,
        provider="bangumi",
        external_user_id="987654321",
    ).first()
    _assert(connection is not None, "UserExternalAccountConnection did not survive the upgrade or restart.")
    _assert(connection.credential_ciphertext == fixture["external_account_ciphertext"], "Encrypted credential changed.")
    _assert(fake_token not in connection.credential_ciphertext, "Database credential contains plaintext token.")
    _assert(decrypt_credentials(connection.credential_ciphertext)["access_token"] == fake_token, "Credential cannot be decrypted.")
    if _migration_applied("journal", "0005_external_collection_sync_state"):
        from journal.models import ExternalCollectionSyncState

        sync_state_id = fixture.get("external_sync_state_id")
        if sync_state_id is None:
            _assert(
                not ExternalCollectionSyncState.objects.filter(identity=identity).exists(),
                "Upgrade synthesized a collection sync baseline for an existing identity.",
            )
        else:
            sync_state = ExternalCollectionSyncState.objects.filter(
                pk=sync_state_id,
                identity=identity,
                connection=connection,
            ).first()
            _assert(sync_state is not None, "ExternalCollectionSyncState did not survive the upgrade or restart.")
            _assert(
                sync_state.baselines == fixture["external_sync_baselines"],
                "ExternalCollectionSyncState partial baseline semantics changed.",
            )
            _assert(
                set(sync_state.baselines) == {"watch_status"},
                "Upgrade synthesized missing score or review baselines.",
            )
            _assert(
                sync_state.last_synced_at.isoformat() == fixture["external_sync_last_synced_at"],
                "ExternalCollectionSyncState last_synced_at changed.",
            )

    integration = IntegrationConnection.objects.filter(pk=fixture["integration_connection_id"]).first()
    _assert(integration is not None and integration.enabled, "IntegrationConnection did not survive.")
    _assert(integration.get_secret() == "stateful-upgrade-integration-secret", "Integration secret cannot be decrypted.")
    _assert(
        ExternalIdentityBinding.objects.filter(
            pk=fixture["integration_binding_id"],
            connection=integration,
            user=user,
            enabled=True,
        ).exists(),
        "ExternalIdentityBinding did not survive.",
    )

    projects = PluginProject.objects.filter(slug=fixture["plugin_slug"])
    _assert(projects.count() == 1, "Official PluginProject is missing or duplicated.")
    project = projects.get()
    _assert(project.pk == fixture["plugin_project_id"], "Official PluginProject identity changed.")
    _assert(project.status == PluginProject.Status.ACTIVE, "Official PluginProject is not active.")

    installation = UserPluginInstallation.objects.filter(pk=fixture["installation_id"], user=user, plugin=project).first()
    _assert(installation is not None and installation.enabled, "UserPluginInstallation is missing or disabled.")
    _assert(installation.config == fixture["installation_config"], "UserPluginInstallation config changed.")
    plugin_data = PluginData.objects.filter(
        pk=fixture["plugin_data_id"],
        user=user,
        plugin=project,
        namespace="fixture_state",
        key="upgrade-gate-record-a",
    ).first()
    _assert(plugin_data is not None and plugin_data.value == fixture["plugin_data_value"], "Non-watch PluginData changed or is missing.")

    core_history_applied = _migration_applied("journal", "0004_core_watch_history_and_metadata_source")
    legacy_history = PluginData.objects.filter(pk=fixture["watch_plugin_data_id"])
    if core_history_applied:
        from journal.models import WatchHistoryRecord

        _assert(not legacy_history.exists(), "Canonical watch_history PluginData was not removed after migration.")
        rows = list(WatchHistoryRecord.objects.filter(entry=journal).order_by("sequence"))
        _assert(len(rows) == len(fixture["watch_plugin_data_value"]), "WatchHistoryRecord count changed during migration.")
        _assert([row.sequence for row in rows] == [1, 2], "WatchHistoryRecord order was not preserved.")
        _assert([row.watched_on.isoformat() for row in rows] == ["2026-07-01", "2026-07-08"], "Watch dates changed.")
        _assert(rows[0].notes == ["stateful-upgrade-note-a"], "Watch history notes changed.")
        _assert(rows[0].metadata == {"source_document_label": "fixture-document-a"}, "Watch history metadata changed.")
        watch_history_status = "MIGRATED_TO_CORE"
    else:
        legacy = legacy_history.first()
        _assert(legacy is not None and legacy.value == fixture["watch_plugin_data_value"], "Base watch_history PluginData changed.")
        watch_history_status = "BASE_PLUGINDATA"

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
    if core_history_applied:
        _assert(deployment.current_version.version == "0.4.0", "Importer 0.4.0 is not active after cutover.")
        _assert(deployment.rollback_floor == "0.4.0", "Importer rollback floor is not 0.4.0.")
    else:
        _assert(deployment.current_version.version == fixture["base_plugin_version"], "Base importer version changed before upgrade.")
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
        "external_media_identity": "PERSISTED",
        "metadata_source": "PASS" if core_history_applied else "NOT_APPLICABLE_BASE",
        "external_account_connection": "PERSISTED",
        "external_collection_sync_state": (
            "PERSISTED_PARTIAL_BASELINE"
            if fixture.get("external_sync_state_id") is not None
            else (
                "ABSENT_UNINITIALIZED"
                if _migration_applied("journal", "0005_external_collection_sync_state")
                else "NOT_APPLICABLE_BASE"
            )
        ),
        "credential_encryption": "PASS",
        "integration": "PASS",
        "user_plugin_installation": "PASS",
        "plugin_data_non_watch": "PASS",
        "watch_history": watch_history_status,
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
