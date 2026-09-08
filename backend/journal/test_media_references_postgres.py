import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from site_config.media_storage.local import DynamicLocalBackend
from site_config.media_storage.usage import managed_usage_bytes
from site_config.models import MediaObject, MediaWriteReservation

from .media_references import cleanup_media, lock_media_owner, poster_usage
from .models import JournalEntry
from .test_media_references import MediaReferenceFixture, poster_upload


@skipUnless(connection.vendor == "postgresql", "PostgreSQL lifecycle serialization requires PostgreSQL")
class MediaReferencePostgreSQLTests(MediaReferenceFixture):
    def test_committed_new_holder_blocks_a_stale_failed_cleanup_retry(self):
        source, media, url = self.upload()
        with patch.object(DynamicLocalBackend, "delete", side_effect=OSError("synthetic-delete-failure")):
            response = self.client.delete(reverse("entry-detail", args=[source.pk]))
        self.assertEqual(response.status_code, 204)
        media.refresh_from_db()
        self.assertEqual(media.lifecycle, MediaObject.Lifecycle.DELETE_FAILED)
        candidate_read, holder_committed = threading.Event(), threading.Event()
        pids, events = {}, []

        def retry():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids["retry"] = cursor.fetchone()[0]
                stale = MediaObject.objects.get(pk=media.pk)
                self.assertEqual(stale.lifecycle, MediaObject.Lifecycle.DELETE_FAILED)
                self.assertFalse(stale.journal_references.exists())
                events.append("stale_unheld_candidate_read")
                candidate_read.set()
                self.assertTrue(holder_committed.wait(10))
                result = cleanup_media(stale.pk)
                events.append("retry_rechecked_after_holder_commit")
                return result
            finally:
                connections.close_all()

        def hold():
            close_old_connections()
            try:
                self.assertTrue(candidate_read.wait(10))
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids["holder"] = cursor.fetchone()[0]
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=self.owner.pk))
                response = client.post(reverse("entry-list"), {"title": "Held before retry", "custom_poster_url": url}, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                self.assertFalse(connection.in_atomic_block)
                events.append("new_holder_committed_201")
                return response
            finally:
                holder_committed.set()
                connections.close_all()

        with patch.object(DynamicLocalBackend, "delete", wraps=DynamicLocalBackend.delete) as delete, ThreadPoolExecutor(max_workers=2) as executor:
            retried = executor.submit(retry)
            held = executor.submit(hold)
            self.assertEqual(held.result(timeout=15).status_code, 201)
            self.assertFalse(retried.result(timeout=15))
            delete.assert_not_called()
        self.assertNotEqual(pids["retry"], pids["holder"])
        self.assertEqual(events, ["stale_unheld_candidate_read", "new_holder_committed_201", "retry_rechecked_after_holder_commit"])
        media.refresh_from_db()
        self.assertEqual(media.lifecycle, MediaObject.Lifecycle.ACTIVE)
        self.assertEqual(media.journal_references.count(), 1)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes)
        self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
        self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
        print(json.dumps({"case": self._testMethodName, "postgres_pids": pids, "events": events,
                          "holder_status": 201, "reclaimed": False, "logical_bytes": media.size_bytes,
                          "physical_bytes": managed_usage_bytes(self.backend)}))

    def wait_for_blocker(self, waiting_pid, blocking_pid):
        expected = set(blocking_pid) if isinstance(blocking_pid, tuple) else {blocking_pid}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid=%s", [waiting_pid])
                row = cursor.fetchone()
            if row and row[0] == "Lock" and expected.intersection(row[1]):
                return {"waiting_pid": waiting_pid, "blockers": row[1]}
            time.sleep(0.02)
        self.fail("Actual PostgreSQL blocking was not observed")

    def run_owner_interleaving(self, first, second):
        acquired, attempted, release = threading.Event(), threading.Event(), threading.Event()
        local = threading.local()
        pids, measurements = {}, []

        def serialized(owner_id):
            if getattr(local, "entered", False):
                return lock_media_owner(owner_id)
            local.entered = True
            started = time.perf_counter()
            if local.index == 1:
                self.assertTrue(acquired.wait(10))
                attempted.set()
            result = lock_media_owner(owner_id)
            locked_at = time.perf_counter()
            if local.index == 0:
                acquired.set()
                self.assertTrue(release.wait(10))
            measurements.append({"worker": local.index, "lock_wait_ms": (locked_at - started) * 1000})
            return result

        def worker(index, operation):
            close_old_connections()
            local.index = index
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[index] = cursor.fetchone()[0]
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=self.owner.pk))
                return operation(client)
            finally:
                connections.close_all()

        observed = None
        with patch("journal.media_references.lock_media_owner", serialized), patch("journal.domain_services.lock_media_owner", serialized), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, index, operation) for index, operation in enumerate((first, second))]
            try:
                self.assertTrue(attempted.wait(10))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pid, wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid=%s", [pids[1]])
                        row = cursor.fetchone()
                    if row and row[1] == "Lock" and pids[0] in row[2]:
                        observed = {"waiting_pid": row[0], "blockers": row[2]}
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(observed, "Actual PostgreSQL blocking was not observed")
            finally:
                release.set()
            responses = [future.result(timeout=20) for future in futures]
        print(json.dumps({"case": self._testMethodName, "pg_lock": observed, "timings": measurements,
                          "statuses": [response.status_code for response in responses],
                          "logical_bytes": poster_usage(self.owner.pk).known_bytes,
                          "physical_bytes": managed_usage_bytes(self.backend)}))
        return responses

    def test_new_holding_commits_before_source_delete_and_protects_bytes(self):
        source, media, url = self.upload()
        responses = self.run_owner_interleaving(
            lambda client: client.post(reverse("entry-list"), {"title": "Held", "custom_poster_url": url}, format="json"),
            lambda client: client.delete(reverse("entry-detail", args=[source.pk])),
        )
        self.assertEqual([response.status_code for response in responses], [201, 204])
        self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes)
        self.assertEqual(media.journal_references.count(), 1)

    def test_cleanup_claim_precedes_new_holding_and_never_returns_dangling_success(self):
        source, media, url = self.upload()
        deleting, release = threading.Event(), threading.Event()
        holding_started = threading.Event()
        pids = {}
        original = DynamicLocalBackend.delete

        def pause_delete(adapter, key):
            deleting.set()
            self.assertTrue(release.wait(10))
            return original(adapter, key)

        def remove():
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids["remove"] = cursor.fetchone()[0]
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=self.owner.pk))
                return client.delete(reverse("entry-detail", args=[source.pk]))
            finally:
                connections.close_all()

        def add_holding():
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids["hold"] = cursor.fetchone()[0]
                holding_started.set()
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=self.owner.pk))
                return client.post(reverse("entry-list"), {"title": "New hold", "custom_poster_url": url}, format="json")
            finally:
                connections.close_all()

        with patch.object(DynamicLocalBackend, "delete", pause_delete), ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(remove)
            try:
                self.assertTrue(deleting.wait(10))
                self.assertEqual(MediaObject.objects.get(pk=media.pk).lifecycle, "deleting")
                holding = executor.submit(add_holding)
                self.assertTrue(holding_started.wait(10))
                observed = self.wait_for_blocker(pids["hold"], pids["remove"])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15).status_code, 204)
            response = holding.result(timeout=15)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(response.data["code"], "media_hold_unavailable")
            print(json.dumps({"case": self._testMethodName, "pg_lock": observed, "hold_status": response.status_code}))
        self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())
        self.assertFalse(DynamicLocalBackend(self.backend).exists(media.object_key))
        self.assertFalse(JournalEntry.objects.exists())

    def test_concurrent_cleanup_retries_cannot_reopen_admission_during_other_delete(self):
        source, media, url = self.upload()
        with patch("journal.media_references.cleanup_media", return_value=False):
            source.delete()
        entered, release, second_started, hold_started = (threading.Event() for _ in range(4))
        pids, calls = {}, []
        original = DynamicLocalBackend.delete

        def deletion(adapter, key):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(10))
                raise OSError("isolated first cleanup failure")
            return original(adapter, key)

        def worker(name, started=None):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[name] = cursor.fetchone()[0]
                if started:
                    started.set()
                if name == "hold":
                    client = APIClient()
                    client.force_authenticate(get_user_model().objects.get(pk=self.owner.pk))
                    return client.post(reverse("entry-list"), {"title": "Retry hold", "custom_poster_url": url}, format="json")
                return cleanup_media(media.pk)
            finally:
                connections.close_all()

        with patch.object(DynamicLocalBackend, "delete", deletion), ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(worker, "first")
            try:
                self.assertTrue(entered.wait(10))
                second = executor.submit(worker, "second", second_started)
                self.assertTrue(second_started.wait(10))
                retry_lock = self.wait_for_blocker(pids["second"], pids["first"])
                holding = executor.submit(worker, "hold", hold_started)
                self.assertTrue(hold_started.wait(10))
                hold_lock = self.wait_for_blocker(pids["hold"], (pids["first"], pids["second"]))
                self.assertEqual(len(calls), 1, "Concurrent adapter deletes bypassed lifecycle serialization")
            finally:
                release.set()
            self.assertFalse(first.result(timeout=15))
            reclaimed = second.result(timeout=15)
            response = holding.result(timeout=15)
        if response.status_code == 201:
            self.assertFalse(reclaimed)
            self.assertEqual(len(calls), 1)
            self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
            self.assertEqual(media.journal_references.count(), 1)
            self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
        else:
            self.assertEqual(response.status_code, 400, response.data)
            self.assertTrue(reclaimed)
            self.assertEqual(len(calls), 2)
            self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())
            self.assertFalse(DynamicLocalBackend(self.backend).exists(media.object_key))
            self.assertEqual(managed_usage_bytes(self.backend), 0)
        print(json.dumps({"case": self._testMethodName, "retry_lock": retry_lock, "hold_lock": hold_lock,
                          "adapter_delete_calls": len(calls), "hold_status": response.status_code, "reclaimed": reclaimed}))

    def test_upload_and_url_holding_compete_for_the_last_quota_in_both_orders(self):
        for upload_first in (True, False):
            with self.subTest(upload_first=upload_first):
                source, media, url = self.upload()
                before = poster_usage(self.owner.pk).known_bytes
                upload = lambda client: client.post(reverse("entry-list"), {"title": "Competing upload", "poster_file": poster_upload()}, format="multipart")
                hold = lambda client: client.post(reverse("entry-list"), {"title": "Competing holding", "custom_poster_url": url}, format="json")
                with override_settings(POSTER_STORAGE_QUOTA_BYTES=before + media.size_bytes):
                    responses = self.run_owner_interleaving(*( (upload, hold) if upload_first else (hold, upload) ))
                self.assertEqual([response.status_code for response in responses], [201, 400])
                self.assertEqual(poster_usage(self.owner.pk).known_bytes, before + media.size_bytes)
                self.assertFalse(MediaWriteReservation.objects.filter(status__in=["pending", "cleanup_ready", "cleanup_failed"]).exists())

    def test_same_slot_retries_and_distinct_slots_have_different_logical_counts(self):
        _source, media, url = self.upload()
        target = JournalEntry.objects.create(user=self.owner, title="Concurrent target")
        update = lambda client: client.patch(reverse("entry-detail", args=[target.pk]), {"custom_poster_url": url}, format="json")
        responses = self.run_owner_interleaving(update, update)
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(target.media_references.count(), 1)
        create = lambda client: client.post(reverse("entry-list"), {"title": "Distinct target", "custom_poster_url": url}, format="json")
        responses = self.run_owner_interleaving(create, create)
        self.assertEqual([response.status_code for response in responses], [201, 201])
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes * 4)
        self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)

    @override_settings(POSTER_STORAGE_QUOTA_BYTES=100)
    def test_two_uploads_cannot_exceed_q100(self):
        create = lambda client: client.post(reverse("entry-list"), {"title": "Quota contender", "poster_file": poster_upload()}, format="multipart")
        responses = self.run_owner_interleaving(create, create)
        self.assertEqual([response.status_code for response in responses], [201, 400])
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 84)

    def test_other_owner_and_non_media_patch_do_not_wait_for_locked_owner(self):
        source, _media, _url = self.upload()

        def worker(owner_id, media_change):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=owner_id))
                if media_change:
                    return client.post(reverse("entry-list"), {"title": "Other owner", "poster_file": poster_upload()}, format="multipart")
                return client.patch(reverse("entry-detail", args=[source.pk]), {"title": "Non-media update"}, format="json")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            with transaction.atomic():
                lock_media_owner(self.owner.pk)
                other = executor.submit(worker, self.other.pk, True)
                non_media = executor.submit(worker, self.owner.pk, False)
                self.assertEqual(other.result(timeout=10).status_code, 201)
                self.assertEqual(non_media.result(timeout=10).status_code, 200)
        self.assertEqual(poster_usage(self.other.pk).known_bytes, 84)
