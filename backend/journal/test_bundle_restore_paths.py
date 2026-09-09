"""Real PostgreSQL restore races through CSV, provider and SDK entry points.

Only provider network data and pause barriers are injected. Production HTTP
parsing, capability/installation checks, import services, owner locks, database
writes, identities, history and receipts run unchanged on separate connections.
Run serially with the existing restore TransactionTestCase suites.
"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from plugin_host.hooks import HookRegistry
from plugin_host.models import PluginProject, UserPluginInstallation
from plugin_host.runtime.context import PluginContext

from journal import test_bundle_restore as fixture
from journal import test_bundle_restore_concurrency as concurrency
from journal.data_bundle import export_data_bundle
from journal.domain_services import JournalEntryService
from journal.external_accounts.services import apply_import_preview
from journal.models import (
    BundleRestoreSession,
    ExternalImportSession,
    ExternalMediaIdentity,
    JournalEntry,
    WatchHistoryRecord,
)
from journal.serializers_entries import JournalEntrySerializer


@skipUnless(connection.vendor == "postgresql", "Entrypoint races require real PostgreSQL owner locks")
class BundleRestoreEntrypointTests(TransactionTestCase):
    # Reuse setup and observation helpers without inheriting existing test cases.
    bundle = fixture.BundleRestoreTests.bundle
    create = fixture.BundleRestoreTests.create
    upload = fixture.BundleRestoreTests.upload
    chunk = fixture.BundleRestoreTests.chunk
    operation = fixture.BundleRestoreTests.operation
    ready = fixture.BundleRestoreTests.ready
    setUp = concurrency.BundleRestoreConcurrencyTests.setUp
    _submit = concurrency.BundleRestoreConcurrencyTests._submit
    _commit = staticmethod(concurrency.BundleRestoreConcurrencyTests._commit)
    _wait_started = concurrency.BundleRestoreConcurrencyTests._wait_started
    _activity = concurrency.BundleRestoreConcurrencyTests._activity
    _wait_for_lock = concurrency.BundleRestoreConcurrencyTests._wait_for_lock
    _assert_owner_locked = concurrency.BundleRestoreConcurrencyTests._assert_owner_locked
    _assert_session_locked = concurrency.BundleRestoreConcurrencyTests._assert_session_locked
    _pause_first_bundle_create = concurrency.BundleRestoreConcurrencyTests._pause_first_bundle_create
    _receipt = concurrency.BundleRestoreConcurrencyTests._receipt
    _report = concurrency.BundleRestoreConcurrencyTests._report

    def _restore_before_writer(self, title, writer, *, source, identity_count):
        payload = self.bundle(count=3)
        state = self.ready(fixture.encoded(payload))
        entered, release = Event(), Event()
        sources = []
        original_create = JournalEntryService.create

        def observe_create(service, serializer, *, source="core"):
            sources.append((service.user.pk, source))
            return original_create(service, serializer, source=source)

        with self._pause_first_bundle_create(entered, release), patch.object(JournalEntryService, "create", observe_create), ThreadPoolExecutor(max_workers=2) as executor:
            committed = self._submit(executor, "restore", lambda client: self._commit(client, state))
            try:
                self.assertTrue(entered.wait(10), "Restore did not reach its first production create")
                self._assert_owner_locked(self.owner)
                self._assert_session_locked(state)
                ordinary = self._submit(executor, source, writer)
                observed_lock = self._wait_for_lock(source, "restore")
                self.assertFalse(ordinary.done(), "Ordinary entry point bypassed the restore owner lock")
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
                self.assertFalse(ExternalMediaIdentity.objects.filter(entry__user=self.owner).exists())
                self.assertFalse(WatchHistoryRecord.objects.filter(entry__user=self.owner).exists())
                self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).receipt, {})
            finally:
                release.set()
            restored = committed.result(timeout=20)
            result = ordinary.result(timeout=20)

        self.assertEqual(restored.status_code, 200, restored.data)
        self.assertEqual(sources.count((self.owner.pk, "bundle")), 3)
        self.assertEqual(sources.count((self.owner.pk, source)), 1)
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 4)
        self.assertEqual(ExternalMediaIdentity.objects.filter(entry__user=self.owner).count(), identity_count)
        self.assertEqual(WatchHistoryRecord.objects.filter(entry__user=self.owner).count(), 3)
        exported = export_data_bundle(user=self.owner)["entries"]
        self.assertEqual([item for item in exported if item["entry"]["title"] != title], payload["entries"])
        receipt = self._receipt(state, 3)
        self.assertEqual(receipt, restored.data["receipt"])
        # A later ordinary writer cannot make replay of a completed restore
        # fail the empty-target check or publish a second set of bundle rows.
        replay = self.operation(state, "commit")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["receipt"], receipt)
        self.assertEqual(export_data_bundle(user=self.owner)["entries"], exported)
        self._report(entrypoint=source, pg_lock=observed_lock, restore_status=200,
                     receipt_created=3, visible_entries=4, identities=identity_count,
                     history_records=3, replay_receipt_unchanged=True)
        return result, JournalEntry.objects.get(user=self.owner, title=title)

    def test_restore_owner_lock_blocks_actual_csv_import_then_preserves_bundle_receipt(self):
        title = "CSV entry after complete restore"
        row = {
            "title": title, "japanese_title": "CSVからの作品", "airing_period": "2026-9",
            "studio": "CSV Studio", "episodes": "12", "description": "CSV description 🌌",
            "tags": "日常,校园", "personal_score": "8.75", "watch_status": "completed",
            "review": 'Quoted "CSV" review\n第二行 🌌', "visibility": "private",
        }
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        raw = output.getvalue().encode("utf-8-sig")

        def import_csv(client):
            upload = SimpleUploadedFile("restore-competitor.csv", raw, content_type="text/csv")
            return client.post("/api/v1/import/", {"file": upload}, format="multipart")

        response, entry = self._restore_before_writer(title, import_csv, source="csv", identity_count=3)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, {"created": 1, "total": 1, "skipped_duplicates": 0, "errors": []})
        for key in ("title", "japanese_title", "airing_period", "studio", "episodes", "description", "watch_status", "review", "visibility"):
            self.assertEqual(getattr(entry, key), row[key])
        self.assertEqual(str(entry.personal_score), "8.75")
        self.assertEqual(entry.tags, ["日常", "校园"])
        self.assertFalse(entry.external_identities.exists())
        self.assertFalse(entry.watch_history_records.exists())

    def _provider_fixture(self):
        external_id = "7000001"
        title = "Provider entry competing with restore"
        remote = {
            "provider": "bangumi", "external_id": external_id, "title": title,
            "japanese_title": "提供元の作品", "remote_status": "completed",
            "remote_rating": 9, "remote_comment": "Provider user review 🌌",
            "remote_updated_at": "2026-09-08T08:00:00+08:00",
        }
        metadata = {
            "title": title, "japanese_title": "提供元の作品", "summary": "Provider summary 🌌",
            "episodes": 12, "air_date": "2026-09-01", "studio": "Provider Studio",
            "tags": ["日常", "原创"], "score": 8.5, "poster_url": "", "thumbnail_url": "",
            "provider_name": "Bangumi", "provider_url": f"https://bgm.tv/subject/{external_id}",
            "external_id": external_id,
        }
        session = ExternalImportSession.objects.create(user=self.owner, provider="bangumi", snapshot=[remote],
                                                       expires_at=timezone.now() + timedelta(minutes=20))

        def fetch_subject(_provider, value, *, force=False):
            self.assertEqual(str(value), external_id)
            return deepcopy(metadata)

        def apply_provider(_client):
            user = get_user_model().objects.get(pk=self.owner.pk)
            return apply_import_preview(user=user, provider_slug="bangumi", preview_id=session.pk,
                                        items=[{"external_id": external_id, "mode": "CREATE_NEW"}])

        network = patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject",
                        autospec=True, side_effect=fetch_subject)
        return title, remote, metadata, session, apply_provider, network

    def _assert_provider(self, result, entry, remote, metadata, session):
        self.assertEqual(result["counts"], {"created": 1, "bound": 0, "updated": 0, "skipped": 0, "conflict": 0, "failed": 0})
        self.assertEqual(result["results"], [{"external_id": remote["external_id"], "status": "created", "entry_id": entry.pk}])
        self.assertEqual(entry.user_id, self.owner.pk)
        self.assertEqual(entry.title, metadata["title"])
        self.assertEqual(entry.japanese_title, metadata["japanese_title"])
        self.assertEqual(entry.description, metadata["summary"])
        self.assertEqual(entry.airing_period, "2026-9")
        self.assertEqual(entry.studio, metadata["studio"])
        self.assertEqual(entry.episodes, str(metadata["episodes"]))
        self.assertEqual(entry.tags, metadata["tags"])
        self.assertEqual(str(entry.personal_score), "9.00")
        self.assertEqual(entry.watch_status, remote["remote_status"])
        self.assertEqual(entry.review, remote["remote_comment"])
        identity = entry.external_identities.get(provider="bangumi", external_id=remote["external_id"])
        self.assertEqual(identity.canonical_url, metadata["provider_url"])
        self.assertEqual({key: value for key, value in identity.metadata.items() if key != "import_provenance"}, metadata)
        provenance = identity.metadata["import_provenance"]
        self.assertEqual(provenance["source"], "bangumi")
        self.assertEqual(provenance["collection_status"], remote["remote_status"])
        self.assertEqual(provenance["collection_updated_at"], remote["remote_updated_at"])
        self.assertTrue(provenance["imported_at"])
        self.assertTrue(identity.is_metadata_source)
        self.assertFalse(entry.watch_history_records.exists())
        session.refresh_from_db()
        self.assertIsNotNone(session.applied_at)
        self.assertEqual(session.snapshot, [])
        self.assertEqual(session.result, result)

    def test_restore_owner_lock_blocks_actual_provider_import_then_preserves_bundle_receipt(self):
        title, remote, metadata, session, apply_provider, network = self._provider_fixture()
        with network as fetched:
            result, entry = self._restore_before_writer(title, apply_provider, source="external-account-import", identity_count=4)
            self._assert_provider(result, entry, remote, metadata, session)
            self.assertEqual(apply_provider(None), result)
            fetched.assert_called_once()
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 4)
        self.assertEqual(ExternalMediaIdentity.objects.filter(entry__user=self.owner).count(), 4)

    def test_restore_owner_lock_blocks_actual_sdk_facade_create_then_preserves_bundle_receipt(self):
        plugin = PluginProject.objects.create(plugin_id="com.example.restore-entrypoint", slug="restore-entrypoint",
                                               name="Restore Entrypoint", description="Synthetic concurrency fixture",
                                               installation_mode=PluginProject.InstallationMode.USER,
                                               status=PluginProject.Status.ACTIVE)
        UserPluginInstallation.objects.create(user=self.owner, plugin=plugin, enabled=True)
        manifest = {
            "schemaVersion": 2, "sdkApi": 2, "id": plugin.plugin_id, "slug": plugin.slug,
            "name": plugin.name, "version": "1.0.0", "installationMode": "user",
            "runtimes": ["backend"], "extensions": ["backend.api"], "coreCapabilities": ["journal"],
            "permissions": [], "hooks": [], "settings": [],
            "dataPolicy": {"storesPersonalData": False, "usesExternalNetwork": False},
        }
        fields = {"title": "SDK entry after complete restore", "japanese_title": "SDKからの作品",
                  "description": "SDK description 🌌", "review": "SDK user review 🌌", "tags": ["日常", "原创"],
                  "personal_score": "8.25", "watch_status": "watching", "visibility": "private"}

        def create_through_sdk(_client):
            context = PluginContext(slug=plugin.slug, version="1.0.0", root=Path(self.temporary.name),
                                    manifest=manifest, hook_registry=HookRegistry())
            actor = SimpleNamespace(user=get_user_model().objects.get(pk=self.owner.pk))
            return context.journal.bind(actor).create_entry(fields)

        dto, entry = self._restore_before_writer(fields["title"], create_through_sdk, source="plugin", identity_count=3)
        self.assertEqual(dto["entry_id"], entry.pk)
        self.assertEqual(entry.user_id, self.owner.pk)
        self.assertEqual(str(entry.personal_score), "8.25")
        for key in ("title", "japanese_title", "description", "review", "tags", "watch_status", "visibility"):
            self.assertEqual(getattr(entry, key), fields[key])
            self.assertEqual(dto[key], fields[key])
        self.assertFalse(entry.external_identities.exists())
        self.assertFalse(entry.watch_history_records.exists())

    def test_provider_import_wins_owner_lock_and_restore_rechecks_empty_target_without_partial_bundle(self):
        payload = self.bundle(count=3)
        state = self.ready(fixture.encoded(payload))
        title, remote, metadata, session, apply_provider, network = self._provider_fixture()
        entered, release = Event(), Event()
        original = JournalEntrySerializer.create

        def paused(serializer, validated_data):
            if validated_data.get("title") == title and validated_data["user"].pk == self.owner.pk:
                self.assertTrue(connection.in_atomic_block)
                entered.set()
                self.assertTrue(release.wait(20), "Provider create barrier was not released")
            return original(serializer, validated_data)

        with network as fetched, patch.object(JournalEntrySerializer, "create", paused), ThreadPoolExecutor(max_workers=2) as executor:
            imported = self._submit(executor, "provider", apply_provider)
            try:
                self.assertTrue(entered.wait(10), "Provider did not reach its production serializer create")
                self._assert_owner_locked(self.owner)
                with transaction.atomic():
                    unlocked = ExternalImportSession.objects.select_for_update(skip_locked=True).filter(pk=session.pk).first()
                    self.assertIsNone(unlocked, "Provider apply did not hold its import session lock")
                committed = self._submit(executor, "restore", lambda client: self._commit(client, state))
                observed_lock = self._wait_for_lock("restore", "provider")
                self.assertFalse(committed.done())
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
                self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).receipt, {})
            finally:
                release.set()
            result = imported.result(timeout=20)
            rejected = committed.result(timeout=20)
            fetched.assert_called_once()

        self.assertEqual(rejected.status_code, 409, rejected.data)
        self.assertEqual(rejected.data["code"], "bundle_import_requires_empty_journal")
        entry = JournalEntry.objects.get(user=self.owner)
        self._assert_provider(result, entry, remote, metadata, session)
        self.assertEqual(ExternalMediaIdentity.objects.filter(entry__user=self.owner).count(), 1)
        self.assertFalse(WatchHistoryRecord.objects.filter(entry__user=self.owner).exists())
        restore_session = BundleRestoreSession.objects.get(pk=state["id"])
        self.assertEqual(restore_session.state, "failed")
        self.assertEqual(restore_session.receipt, {})
        self.assertEqual(export_data_bundle(user=self.source)["entries"], payload["entries"])
        self._report(entrypoint="external-account-import", pg_lock=observed_lock, restore_status=409,
                     error_code="bundle_import_requires_empty_journal", visible_entries=1,
                     identities=1, history_records=0, receipt={})
