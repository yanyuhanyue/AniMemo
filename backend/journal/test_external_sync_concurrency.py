import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from journal.external_accounts.credentials import encrypt_credentials
from journal.external_sync.errors import ExternalSyncError
from journal.external_sync.services import (
    apply_collection_sync,
    preview_collection_sync,
)
from journal.models import (
    ExternalCollectionSyncState,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
)

User = get_user_model()


class ExternalCollectionSyncStatePostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL-only collection sync concurrency gate")
        self.user = User.objects.create_user(username="sync-race", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(user=self.user, title="并发同步状态")
        self.identity = ExternalMediaIdentity.objects.create(
            entry=self.entry,
            provider="bangumi",
            external_id="1424",
            canonical_url="https://bgm.tv/subject/1424",
        )
        self.connection = UserExternalAccountConnection.objects.create(
            user=self.user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
            external_user_id="100",
            external_username="sync-race",
            credential_ciphertext=encrypt_credentials(
                {"access_token": "race-access-token", "token_type": "Bearer"}
            ),
            connected_at=timezone.now(),
        )

    @skipUnlessDBFeature("supports_transactions")
    def test_concurrent_creation_keeps_one_state_per_identity(self):
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                ExternalCollectionSyncState.objects.create(
                    identity_id=self.identity.pk,
                    connection_id=self.connection.pk,
                )
                outcome = "created"
            except (IntegrityError, ValidationError):
                outcome = "duplicate"
            finally:
                connections.close_all()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sorted(outcomes), ["created", "duplicate"])
        self.assertEqual(ExternalCollectionSyncState.objects.filter(identity=self.identity).count(), 1)

    def test_database_cascades_state_for_identity_and_connection_lifecycle(self):
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.connection,
        )
        self.connection.delete()
        self.assertFalse(ExternalCollectionSyncState.objects.filter(pk=state.pk).exists())
        self.assertTrue(JournalEntry.objects.filter(pk=self.entry.pk).exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_postgresql_stale_identity_context_is_rejected(self, get_collection):
        def rebind(*args, **kwargs):
            ExternalMediaIdentity.objects.filter(pk=self.identity.pk).update(external_id="456")
            return {
                "provider": "bangumi",
                "external_id": "1424",
                "remote_status": "watching",
                "remote_rating": None,
                "remote_rating_present": False,
                "remote_comment": "",
                "remote_comment_present": False,
            }

        get_collection.side_effect = rebind
        with self.assertRaises(ExternalSyncError) as caught:
            preview_collection_sync(
                user=self.user,
                provider_slug="bangumi",
                entry_id=self.entry.pk,
            )
        self.assertEqual(caught.exception.detail["code"], "sync_context_changed")

    @staticmethod
    def remote(*, watch_status="completed"):
        return {
            "provider": "bangumi",
            "external_id": "1424",
            "remote_status": watch_status,
            "remote_rating": None,
            "remote_rating_present": False,
            "remote_comment": "",
            "remote_comment_present": True,
        }

    def _preview_token(self, get_collection):
        get_collection.return_value = self.remote()
        return preview_collection_sync(
            user=self.user,
            provider_slug="bangumi",
            entry_id=self.entry.pk,
        )["preview_token"]

    def _run_apply_threads(self, tokens, get_collection):
        barrier = threading.Barrier(len(tokens))
        outcomes = []
        lock = threading.Lock()

        def fetch(*_args, **_kwargs):
            barrier.wait(timeout=10)
            return self.remote()

        get_collection.side_effect = fetch

        def worker(token):
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                apply_collection_sync(
                    user=user,
                    provider_slug="bangumi",
                    entry_id=self.entry.pk,
                    preview_token=token,
                    actions=[{"field": "watch_status", "action": "pull_remote"}],
                )
                outcome = "success"
            except ExternalSyncError as error:
                outcome = str(error.detail["code"])
            finally:
                connections.close_all()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker, args=(token,)) for token in tokens]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        return outcomes

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_concurrent_same_preview_token_has_one_success_and_one_stale(self, get_collection):
        token = self._preview_token(get_collection)
        outcomes = self._run_apply_threads([token, token], get_collection)

        self.assertEqual(sorted(outcomes), ["success", "sync_preview_stale"])
        self.entry.refresh_from_db()
        state = ExternalCollectionSyncState.objects.get(identity=self.identity)
        self.assertEqual(self.entry.watch_status, "completed")
        self.assertEqual(state.baselines, {"watch_status": {"present": True, "value": "completed"}})
        self.assertIsNotNone(state.last_synced_at)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_concurrent_distinct_previews_cannot_lose_baseline_advancement(self, get_collection):
        first = self._preview_token(get_collection)
        second = self._preview_token(get_collection)
        outcomes = self._run_apply_threads([first, second], get_collection)

        self.assertEqual(sorted(outcomes), ["success", "sync_preview_stale"])
        self.assertEqual(ExternalCollectionSyncState.objects.filter(identity=self.identity).count(), 1)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_concurrent_local_edit_during_remote_get_is_not_overwritten(self, get_collection):
        token = self._preview_token(get_collection)
        remote_started = threading.Event()
        continue_remote = threading.Event()
        outcome = []

        def fetch(*_args, **_kwargs):
            remote_started.set()
            self.assertTrue(continue_remote.wait(timeout=10))
            return self.remote()

        get_collection.side_effect = fetch

        def worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                apply_collection_sync(
                    user=user,
                    provider_slug="bangumi",
                    entry_id=self.entry.pk,
                    preview_token=token,
                    actions=[{"field": "watch_status", "action": "pull_remote"}],
                )
                outcome.append("success")
            except ExternalSyncError as error:
                outcome.append(str(error.detail["code"]))
            finally:
                connections.close_all()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(remote_started.wait(timeout=10))
        JournalEntry.objects.filter(pk=self.entry.pk).update(
            watch_status="dropped",
            review="并发用户编辑",
        )
        continue_remote.set()
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())

        self.assertEqual(outcome, ["sync_preview_stale"])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.watch_status, "dropped")
        self.assertEqual(self.entry.review, "并发用户编辑")
        self.assertFalse(ExternalCollectionSyncState.objects.exists())
