"""Real PostgreSQL oracles for portable restore and ordinary API writers.

Injected events only pause production calls before a business write or parser
entry. No lock, transaction, database result, or HTTP response is substituted.
Two workers use independent Django connections; the main connection observes
PostgreSQL blocking and committed state. This suite must run serially with other
TransactionTestCase suites sharing its test database.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from site_config.models import InstallationState

from journal import test_bundle_restore as fixture
from journal.data_bundle import export_data_bundle, restore
from journal.data_bundle import restore_storage as storage
from journal.domain_services import JournalEntryService
from journal.models import (
    BundleRestoreSession,
    ExternalMediaIdentity,
    JournalEntry,
    WatchHistoryRecord,
)
from journal.serializers_entries import JournalEntrySerializer


@skipUnless(connection.vendor == "postgresql", "Restore concurrency requires real PostgreSQL")
class BundleRestoreConcurrencyTests(TransactionTestCase):
    # Reuse setup and transport helpers without inheriting the functional tests.
    bundle = fixture.BundleRestoreTests.bundle
    create = fixture.BundleRestoreTests.create
    upload = fixture.BundleRestoreTests.upload
    chunk = fixture.BundleRestoreTests.chunk
    operation = fixture.BundleRestoreTests.operation
    ready = fixture.BundleRestoreTests.ready

    def setUp(self):
        fixture.BundleRestoreTests.setUp(self)
        installation, _ = InstallationState.objects.get_or_create(pk=1)
        installation.status = InstallationState.Status.INITIALIZED
        installation.save(update_fields=["status"])
        self.pids = {}
        self.started = {}
        self.elapsed_ms = {}

    def _submit(self, executor, name, operation, *, owner=None):
        started = self.started[name] = Event()
        owner_id = (owner or self.owner).pk

        def worker():
            close_old_connections()
            began = monotonic()
            try:
                with connections["default"].cursor() as cursor:
                    # Bound deadlocks/regressions without replacing production
                    # locking. These settings belong only to this worker socket.
                    cursor.execute("SET lock_timeout = '15s'")
                    cursor.execute("SET statement_timeout = '20s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    self.pids[name] = cursor.fetchone()[0]
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=owner_id))
                started.set()
                return operation(client)
            finally:
                self.elapsed_ms[name] = round((monotonic() - began) * 1000, 3)
                connections.close_all()

        return executor.submit(worker)

    @staticmethod
    def _commit(client, state):
        return client.post(
            f'/api/v1/bundle-restores/{state["id"]}/commit/',
            {"generation": state["generation"]}, format="json",
        )

    @staticmethod
    def _create_entry(client, title):
        return client.post("/api/v1/entries/", {"title": title}, format="json")

    def _wait_started(self, name):
        self.assertTrue(self.started[name].wait(10), f"{name} did not start its database connection")

    def _activity(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pid, wait_event_type, pg_blocking_pids(pid) "
                "FROM pg_stat_activity WHERE pid = %s", [self.pids[name]],
            )
            row = cursor.fetchone()
        if row:
            return {"waiting_pid": row[0], "wait_event_type": row[1], "blocking_pids": row[2]}
        return None

    def _wait_for_lock(self, waiter, blocker, *, early_response=None):
        self._wait_started(waiter)
        self.assertNotEqual(self.pids[waiter], self.pids[blocker])
        deadline = monotonic() + 10
        while monotonic() < deadline:
            observed = self._activity(waiter)
            if observed and observed["wait_event_type"] == "Lock" and self.pids[blocker] in observed["blocking_pids"]:
                return observed
            if early_response is not None and early_response.done():
                response = early_response.result(timeout=1)
                self.assertEqual(response.status_code, 409, response.data)
                return {"early_status": 409, "waiting_pid": self.pids[waiter]}
            sleep(0.02)
        self.fail("Expected PostgreSQL Lock wait and blocking PID were not observed")

    def _assert_owner_locked(self, owner):
        with transaction.atomic():
            unlocked = get_user_model().objects.select_for_update(skip_locked=True).filter(pk=owner.pk).first()
            self.assertIsNone(unlocked, "The paused writer did not hold its actual owner row lock")

    def _assert_session_locked(self, state):
        with transaction.atomic():
            unlocked = BundleRestoreSession.objects.select_for_update(skip_locked=True).filter(pk=state["id"]).first()
            self.assertIsNone(unlocked, "The paused operation did not hold its actual session row lock")

    def _pause_first_bundle_create(self, entered, release):
        original = JournalEntryService.create_from_fields

        def paused(service, *args, **kwargs):
            if service.user.pk == self.owner.pk and kwargs.get("source") == "bundle" and not entered.is_set():
                self.assertTrue(connection.in_atomic_block)
                entered.set()
                self.assertTrue(release.wait(20), "Commit barrier was not released")
            return original(service, *args, **kwargs)

        return patch.object(JournalEntryService, "create_from_fields", paused)

    def _receipt(self, state, expected_count):
        status = self.client.get(f'/api/v1/bundle-restores/{state["id"]}/')
        self.assertEqual(status.status_code, 200, status.data)
        self.assertEqual(status.data["state"], "completed")
        self.assertEqual(status.data["receipt"]["created"], expected_count)
        self.assertEqual(status.data["receipt"]["total"], expected_count)
        self.assertEqual(BundleRestoreSession.objects.filter(owner=self.owner, state="completed").count(), 1)
        self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).receipt, status.data["receipt"])
        return status.data["receipt"]

    def _report(self, **result):
        print("BUNDLE_RESTORE_PG " + json.dumps({
            "case": self._testMethodName, "postgres_pids": self.pids,
            "elapsed_ms": self.elapsed_ms, **result,
        }, sort_keys=True), flush=True)

    def test_commit_blocks_same_owner_api_create_until_complete_bundle_and_receipt(self):
        payload = self.bundle(count=3)
        state = self.ready(fixture.encoded(payload))
        entered, release = Event(), Event()
        title = "ordinary-create-after-restore"
        with self._pause_first_bundle_create(entered, release), ThreadPoolExecutor(max_workers=2) as executor:
            committed = self._submit(executor, "commit", lambda client: self._commit(client, state))
            try:
                self.assertTrue(entered.wait(10), "Commit never reached the first business create")
                self._assert_owner_locked(self.owner)
                self._assert_session_locked(state)
                ordinary = self._submit(executor, "ordinary", lambda client: self._create_entry(client, title))
                lock = self._wait_for_lock("ordinary", "commit")
                self.assertFalse(ordinary.done())
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
                self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).receipt, {})
            finally:
                release.set()
            restored_response = committed.result(timeout=20)
            ordinary_response = ordinary.result(timeout=20)
        self.assertEqual(restored_response.status_code, 200, restored_response.data)
        self.assertEqual(ordinary_response.status_code, 201, ordinary_response.data)
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 4)
        exported = export_data_bundle(user=self.owner)["entries"]
        self.assertEqual([item for item in exported if item["entry"]["title"] != title], payload["entries"])
        receipt = self._receipt(state, 3)
        self.assertEqual(receipt, restored_response.data["receipt"])
        self._report(pg_lock=lock, statuses=[200, 201], created=receipt["created"], visible_entries=4)

    def test_ordinary_api_create_wins_owner_lock_and_restore_fails_without_partial_rows(self):
        state = self.ready(fixture.encoded(self.bundle(count=3)))
        entered, release = Event(), Event()
        title = "ordinary-create-before-restore"
        original = JournalEntrySerializer.create

        def paused(serializer, validated_data):
            if validated_data.get("title") == title and validated_data["user"].pk == self.owner.pk:
                self.assertTrue(connection.in_atomic_block)
                entered.set()
                self.assertTrue(release.wait(20), "Ordinary create barrier was not released")
            return original(serializer, validated_data)

        with patch.object(JournalEntrySerializer, "create", paused), ThreadPoolExecutor(max_workers=2) as executor:
            ordinary = self._submit(executor, "ordinary", lambda client: self._create_entry(client, title))
            try:
                self.assertTrue(entered.wait(10), "Ordinary API create never acquired its owner lock")
                self._assert_owner_locked(self.owner)
                committed = self._submit(executor, "commit", lambda client: self._commit(client, state))
                lock = self._wait_for_lock("commit", "ordinary")
                self.assertFalse(committed.done())
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
            finally:
                release.set()
            ordinary_response = ordinary.result(timeout=20)
            restored_response = committed.result(timeout=20)
        self.assertEqual(ordinary_response.status_code, 201, ordinary_response.data)
        self.assertEqual(restored_response.status_code, 409, restored_response.data)
        self.assertEqual(restored_response.data["code"], "bundle_import_requires_empty_journal")
        self.assertEqual(list(JournalEntry.objects.filter(user=self.owner).values_list("title", flat=True)), [title])
        self.assertFalse(ExternalMediaIdentity.objects.filter(entry__user=self.owner).exists())
        self.assertFalse(WatchHistoryRecord.objects.filter(entry__user=self.owner).exists())
        session = BundleRestoreSession.objects.get(pk=state["id"])
        self.assertEqual(session.receipt, {})
        self.assertEqual(session.state, "failed")
        self._report(pg_lock=lock, statuses=[201, 409], visible_entries=1, receipt=session.receipt)

    def test_concurrent_commits_publish_once_and_resolve_to_one_receipt(self):
        payload = self.bundle(count=3)
        state = self.ready(fixture.encoded(payload))
        entered, release = Event(), Event()
        with self._pause_first_bundle_create(entered, release), ThreadPoolExecutor(max_workers=2) as executor:
            first = self._submit(executor, "first_commit", lambda client: self._commit(client, state))
            try:
                self.assertTrue(entered.wait(10), "First commit did not reach its atomic apply")
                self._assert_session_locked(state)
                second = self._submit(executor, "second_commit", lambda client: self._commit(client, state))
                observed = self._wait_for_lock("second_commit", "first_commit", early_response=second)
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
                self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).receipt, {})
            finally:
                release.set()
            first_response = first.result(timeout=20)
            second_response = second.result(timeout=20)
        self.assertEqual(first_response.status_code, 200, first_response.data)
        self.assertIn(second_response.status_code, (200, 409), second_response.data)
        receipt = self._receipt(state, 3)
        self.assertEqual(first_response.data["receipt"], receipt)
        if second_response.status_code == 200:
            self.assertEqual(second_response.data["receipt"], receipt)
        retried = self.operation(state, "commit")
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(retried.data["receipt"], receipt)
        self.assertEqual(export_data_bundle(user=self.owner)["entries"], payload["entries"])
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 3)
        self.assertEqual(ExternalMediaIdentity.objects.filter(entry__user=self.owner).count(), 3)
        self.assertEqual(WatchHistoryRecord.objects.filter(entry__user=self.owner).count(), 3)
        self._report(pg_lock=observed, statuses=[first_response.status_code, second_response.status_code], created=3)

    @override_settings(BUNDLE_RESTORE_OPERATION_SECONDS=1)
    def test_validation_session_lock_makes_cleanup_skip_locked_preserve_files(self):
        data = fixture.encoded(self.bundle(count=2))
        state = self.upload(data, self.create(data))
        entered, release = Event(), Event()
        original = restore._validate_to_file
        session = BundleRestoreSession.objects.get(pk=state["id"])
        directory = storage.session_directory(session.pk)
        before = {path.name: storage.file_digest(path) for path in directory.iterdir()}
        reserved = session.reserved_bytes

        def paused(current):
            self.assertTrue(connection.in_atomic_block)
            entered.set()
            self.assertTrue(release.wait(20), "Validation barrier was not released")
            return original(current)

        with patch.object(restore, "_validate_to_file", paused), ThreadPoolExecutor(max_workers=2) as executor:
            validated = self._submit(executor, "validate", lambda client: client.post(
                f'/api/v1/bundle-restores/{state["id"]}/validate/',
                {"generation": state["generation"]}, format="json",
            ))
            try:
                self.assertTrue(entered.wait(10), "Validation never acquired the session lock")
                self._assert_session_locked(state)
                # Validation must release its short claim's owner lock. This
                # also establishes that cleanup skips the session, not owner.
                with transaction.atomic():
                    owner = get_user_model().objects.select_for_update(nowait=True).get(pk=self.owner.pk)
                    self.assertEqual(owner.pk, self.owner.pk)
                # A healthy active session is not a cleanup candidate. Let the
                # real one-second lease expire while its operation still holds
                # the session lock, then require cleanup to skip that row.
                sleep(1.1)
                session.refresh_from_db()
                self.assertLessEqual(session.operation_expires_at, timezone.now())
                cleaned = self._submit(executor, "cleanup", lambda _client: restore.cleanup_restores())
                result = cleaned.result(timeout=5)
                self.assertEqual(result["deferred"], 1, result)
                self.assertEqual(result["inspected"], 0, result)
                self.assertEqual(result["cleaned"], 0, result)
                self.assertEqual(result["expired"], 0, result)
                self.assertFalse(validated.done())
                self.assertEqual({path.name: storage.file_digest(path) for path in directory.iterdir()}, before)
                session.refresh_from_db()
                self.assertEqual(session.state, "validating")
                self.assertEqual(session.reserved_bytes, reserved)
            finally:
                release.set()
            response = validated.result(timeout=20)
        self.assertEqual(response.status_code, 408, response.data)
        self.assertEqual(response.data["code"], "request_timeout")
        session.refresh_from_db()
        self.assertEqual(session.state, "failed")
        self.assertEqual(session.receipt, {})
        self.assertEqual(session.reserved_bytes, reserved)
        self.assertNotEqual(self.pids["validate"], self.pids["cleanup"])
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
        self._report(cleanup=result, validation_status=408, raw_files_preserved=len(before), lease_seconds=1)

    def test_different_owner_api_create_completes_while_restore_is_paused(self):
        state = self.ready(fixture.encoded(self.bundle(count=2)))
        other = get_user_model().objects.create_user(username="restore-independent-owner")
        entered, release = Event(), Event()
        with self._pause_first_bundle_create(entered, release), ThreadPoolExecutor(max_workers=2) as executor:
            committed = self._submit(executor, "commit", lambda client: self._commit(client, state))
            try:
                self.assertTrue(entered.wait(10), "Restore did not reach its atomic apply")
                self._assert_owner_locked(self.owner)
                ordinary = self._submit(
                    executor, "other_owner", lambda client: self._create_entry(client, "independent-owner-create"),
                    owner=other,
                )
                response = ordinary.result(timeout=5)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertFalse(committed.done())
                self.assertTrue(JournalEntry.objects.filter(user=other, title="independent-owner-create").exists())
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
            finally:
                release.set()
            restored_response = committed.result(timeout=20)
        self.assertEqual(restored_response.status_code, 200, restored_response.data)
        self.assertNotEqual(self.pids["commit"], self.pids["other_owner"])
        self._receipt(state, 2)
        self.assertEqual(JournalEntry.objects.filter(user=other).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 2)
        self._report(statuses=[200, 201], independent_owner_completed_before_release=True)
