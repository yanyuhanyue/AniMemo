from __future__ import annotations

import json
import os
from uuid import UUID

from config.credentials import CredentialCipher, CredentialCipherError
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from journal.external_sync.canonical import validate_baselines
from journal.models import (
    ExternalCollectionSyncState,
    ExternalMediaIdentity,
    ExternalProviderConfiguration,
    UserExternalAccountConnection,
    WatchHistoryRecord,
)
from journal.watch_history.validation import semantic_digest_from_values
from plugin_host.models import PluginData, PluginVersion

from site_config.models import (
    CloudflareR2Account,
    InstallationState,
    MediaStorageBackend,
    MediaWriteReservation,
    SiteSettings,
)


class Command(BaseCommand):
    help = (
        "Run fixed, secret-free post-Restore data and Memory Integrity probes "
        "and emit one machine-readable result."
    )

    def _instance_identity(self) -> bool:
        value = os.environ.get("ANIMEMO_INSTANCE_ID", "")
        try:
            return str(UUID(value)) == value
        except (TypeError, ValueError, AttributeError):
            return False

    @staticmethod
    def _protection_decryptable() -> bool:
        encrypted_values = []
        site = SiteSettings.objects.filter(pk=1).first()
        if site is not None:
            encrypted_values.extend(
                value
                for value in (
                    site.resend_api_key_encrypted,
                    site.turnstile_secret_encrypted,
                )
                if value
            )
        encrypted_values.extend(
            ExternalProviderConfiguration.objects.exclude(
                encrypted_client_secret=""
            ).values_list("encrypted_client_secret", flat=True)
        )
        encrypted_values.extend(
            UserExternalAccountConnection.objects.exclude(
                credential_ciphertext=""
            ).values_list("credential_ciphertext", flat=True)
        )
        encrypted_values.extend(
            CloudflareR2Account.objects.exclude(
                encrypted_analytics_token=""
            ).values_list("encrypted_analytics_token", flat=True)
        )
        encrypted_values.extend(
            MediaStorageBackend.objects.exclude(
                encrypted_access_key_id=""
            ).values_list("encrypted_access_key_id", flat=True)
        )
        encrypted_values.extend(
            MediaStorageBackend.objects.exclude(
                encrypted_secret_access_key=""
            ).values_list("encrypted_secret_access_key", flat=True)
        )
        try:
            return all(bool(CredentialCipher.decrypt(value)) for value in encrypted_values)
        except (CredentialCipherError, TypeError, ValueError):
            return False

    @staticmethod
    def _authentication_epoch() -> bool:
        state = InstallationState.objects.filter(pk=1).first()
        if state is None:
            return False
        epoch = str(state.authentication_epoch or "")
        return (
            len(epoch) == 64
            and all(character in "0123456789abcdef" for character in epoch)
            and not Session.objects.exists()
        )

    @staticmethod
    def _durable_write() -> bool:
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE site_config_installationstate "
                        "SET authentication_epoch = authentication_epoch WHERE id = 1"
                    )
                    passed = cursor.rowcount == 1
                transaction.set_rollback(True)
            return passed
        except Exception:  # noqa: BLE001 - only a stable boolean leaves this probe
            return False

    @staticmethod
    def _mi1_external_metadata() -> bool:
        return not ExternalMediaIdentity.objects.filter(
            provider=""
        ).exists() and not ExternalMediaIdentity.objects.filter(
            external_id=""
        ).exists() and all(
            isinstance(value, dict)
            for value in ExternalMediaIdentity.objects.values_list(
                "metadata", flat=True
            ).iterator()
        )

    @staticmethod
    def _mi2_provider_identity() -> bool:
        for state in ExternalCollectionSyncState.objects.select_related(
            "identity__entry", "connection"
        ).iterator():
            if (
                state.identity.provider != state.connection.provider
                or state.identity.entry.user_id != state.connection.user_id
            ):
                return False
            try:
                validate_baselines(state.baselines)
            except ValueError:
                return False
        return True

    @staticmethod
    def _mi3_merge_history() -> bool:
        for record in WatchHistoryRecord.objects.iterator():
            expected = semantic_digest_from_values(
                record.watched_on,
                record.brush_label,
                record.episode_start,
                record.episode_end,
            )
            if (
                record.semantic_key != expected
                or not isinstance(record.notes, list)
                or not isinstance(record.metadata, dict)
            ):
                return False
        return True

    @staticmethod
    def _mi4_unsupported_payload() -> bool:
        # Read every opaque plugin payload and immutable manifest without
        # normalizing or writing it.  Exact artifact/member preservation is
        # checked by the host; this probe proves the restored database retained
        # valid JSON values, including fields unknown to the current runtime.
        values = (
            PluginVersion.objects.values_list("manifest_snapshot", flat=True),
            PluginVersion.objects.values_list("runtime_types", flat=True),
            PluginData.objects.values_list("value", flat=True),
        )
        try:
            for queryset in values:
                for value in queryset.iterator():
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _mi5_destructive_ambiguity() -> bool:
        return not MediaWriteReservation.objects.filter(
            status=MediaWriteReservation.Status.PENDING
        ).exists()

    def handle(self, *args, **options):
        del args, options
        checks = {
            "instance.identity": self._instance_identity(),
            "protection.decryptability": self._protection_decryptable(),
            "authentication.epoch": self._authentication_epoch(),
            "durable.write": self._durable_write(),
            "memory.mi1.external_metadata": self._mi1_external_metadata(),
            "memory.mi2.provider_identity": self._mi2_provider_identity(),
            "memory.mi3.merge_history": self._mi3_merge_history(),
            "memory.mi4.unsupported_payload": self._mi4_unsupported_payload(),
            "memory.mi5.destructive_ambiguity": self._mi5_destructive_ambiguity(),
        }
        if not all(checks.values()):
            raise CommandError("RESTORE_INTEGRITY_VALIDATION_FAILED")
        self.stdout.write(json.dumps({"checks": checks}, sort_keys=True))
