"""Real-byte differential and resource checks for one read-only inventory."""

import inspect
import hashlib
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from site_config.media_storage.identity import url_identity_digest
from site_config.media_storage.local import DynamicLocalBackend
from site_config.media_storage.pool import StoragePoolService
from site_config.models import MediaObject, SiteSettings

from .media_inventory import inspect_media_references
from .models import Column, JournalEntry, JournalMediaReference, UserSettings
from .test_media_references import MediaReferenceFixture


class MediaInventoryContextTests(MediaReferenceFixture):
    def require_postgresql(self):
        if connection.vendor != "postgresql":
            self.skipTest("Read context requires a top-level PostgreSQL snapshot")

    def seed_distinct_local(self, count):
        source, first, url = self.upload()
        payload = DynamicLocalBackend(self.backend).path_for(first.object_key).read_bytes()
        JournalEntry.objects.filter(pk=source.pk).update(custom_poster_url=url)
        JournalMediaReference.objects.create(owner=self.owner, entry=source, media=first,
                                            slot="custom_poster_url", value=url)
        objects, entries, refs = [first], [source], []
        for number in range(1, count):
            key = f"context-fixture/{number}.png"
            DynamicLocalBackend(self.backend).write(key, payload, content_type="image/png")
            current_url = self.backend.local_public_base_url + "/" + key
            media = MediaObject(storage_backend=self.backend, object_key=key,
                                size_bytes=first.size_bytes, sha256=first.sha256,
                                upload_owner=self.owner, public_url_snapshot=current_url,
                                public_url_identity=url_identity_digest(current_url),
                                reference_inventory_complete=True)
            objects.append(media)
            entries.append(JournalEntry(user=self.owner, title=f"context-{number}",
                                        poster_file=media.reference_name, custom_poster_url=current_url,
                                        deleted_at=timezone.now() if number % 4 == 0 else None,
                                        review="unrelated long review " * 500))
        MediaObject.objects.bulk_create(objects[1:])
        JournalEntry.objects.bulk_create(entries[1:])
        for entry, media in zip(entries[1:], objects[1:]):
            for slot, value in (("poster_file", media.reference_name), ("custom_poster_url", media.public_url_snapshot)):
                refs.append(JournalMediaReference(owner=self.owner, entry=entry, media=media, slot=slot, value=value))
        JournalMediaReference.objects.bulk_create(refs)
        return objects, entries

    def fresh_report(self):
        # The pre-change implementation is the RED oracle for this resource test.
        if "use_read_context" not in inspect.signature(inspect_media_references).parameters:
            return inspect_media_references()
        return inspect_media_references(use_read_context=False)

    def test_distinct_real_local_inventory_has_bounded_queries_and_matches_fresh(self):
        self.require_postgresql()
        objects, _entries = self.seed_distinct_local(50)
        expected = self.fresh_report()
        with CaptureQueriesContext(connection) as queries:
            actual = inspect_media_references()
        for key in ("slots", "usage", "objects", "other_roles", "unresolved_writes"):
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["status"], "READY")
        self.assertEqual(actual["usage"][0]["known_bytes"], 100 * objects[0].size_bytes)
        self.assertLessEqual(len(queries), 30)

    def assert_fresh_equivalent(self):
        expected = self.fresh_report()
        actual = inspect_media_references()
        self.assertEqual(actual, expected)
        return actual

    def test_501_distinct_local_objects_keep_query_count_bounded(self):
        self.require_postgresql()
        objects, _entries = self.seed_distinct_local(501)
        expected = self.fresh_report()
        with CaptureQueriesContext(connection) as queries:
            actual = inspect_media_references()
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual["slots"]), 1002)
        self.assertEqual(actual["usage"][0]["known_bytes"], 1002 * objects[0].size_bytes)
        self.assertTrue(any(row["soft_deleted"] for row in actual["slots"]))
        self.assertLessEqual(len(queries), 30)
        for row in queries:
            if '"journal_journalentry"' in row["sql"]:
                self.assertNotIn('"review"', row["sql"])
                self.assertNotIn('"description"', row["sql"])

    def test_legacy_unknown_bytes_owner_conflicts_and_mismatches_match_fresh(self):
        self.require_postgresql()
        objects, entries = self.seed_distinct_local(8)
        # Fixture-only old-schema state: leave all business strings and actual
        # files in place, while removing the newer holding inventory rows.
        MediaObject.objects.update(reference_inventory_complete=False)
        JournalMediaReference.objects.filter(entry_id__in=[entry.pk for entry in entries[:5]])._raw_delete("default")
        MediaObject.objects.filter(pk=objects[0].pk).update(upload_owner=None)
        JournalEntry.objects.filter(pk=entries[0].pk).update(poster_file="media-objects/" + str(objects[0].pk).upper())
        MediaObject.objects.filter(pk=objects[1].pk).update(sha256="")
        DynamicLocalBackend(self.backend).path_for(objects[2].object_key).unlink()
        MediaObject.objects.filter(pk=objects[3].pk).update(lifecycle=MediaObject.Lifecycle.DELETING)
        conflict = JournalEntry(user=self.other, title="Conflicting legacy owner", poster_file=objects[4].reference_name)
        JournalEntry.objects.bulk_create([conflict])
        JournalMediaReference.objects.filter(entry=entries[5], slot="custom_poster_url").update(owner=self.other)
        JournalMediaReference.objects.filter(entry=entries[6], slot="poster_file").update(value="mismatched persisted value")
        JournalEntry.objects.filter(pk=entries[7].pk).update(poster_file="media-objects/" + str(uuid.uuid4()))
        report = self.assert_fresh_equivalent()
        self.assertEqual(report["status"], "BLOCKED")
        slot_issues = {row["issue"] for row in report["slots"]}
        self.assertTrue({"OWNER_UNPROVEN", "UNKNOWN_USAGE", "MISSING_OR_INVALID_BYTES", "OWNER_CONFLICT", "MISSING_MEDIA_ROW"} <= slot_issues)
        self.assertEqual(report["usage"][0]["status"], "UNKNOWN_USAGE")

    def test_shared_url_union_history_snapshot_collisions_and_malformed_urls(self):
        self.require_postgresql()
        objects, entries = self.seed_distinct_local(3)
        old_base = self.backend.local_public_base_url
        old_url = objects[1].public_url_snapshot
        MediaObject.objects.filter(pk=objects[2].pk).update(public_url_snapshot=old_url, public_url_identity=url_identity_digest(old_url))
        type(self.backend).objects.filter(pk=self.backend.pk).update(local_public_base_url="https://new.example.test/assets")
        urls = [old_url + "?v=2#poster", old_url.replace("https://", "HTTPS://").replace(".test/", ".test:443/"),
                old_url.replace(".test/", ".test:0/"), old_url.replace("https://", "https://@"),
                old_url.replace("/context-fixture/", "/context-fixture%2f"), old_url + "/../x"]
        JournalEntry.objects.bulk_create([JournalEntry(user=self.owner, title=f"URL variant {index}", custom_poster_url=url)
                                          for index, url in enumerate(urls)])
        with override_settings(MEDIA_HISTORICAL_PUBLIC_BASES={str(self.backend.pk): [old_base]}):
            report = self.assert_fresh_equivalent()
        ambiguous = [row for row in report["slots"] if row["issue"] == "AMBIGUOUS_URL"]
        self.assertTrue(ambiguous)
        self.assertTrue(all(row["candidate_ids"] == sorted([str(objects[1].pk), str(objects[2].pk)]) for row in ambiguous))
        self.assertTrue(any(row["issue"] == "LINK_ONLY" for row in report["slots"]))

    def test_other_roles_supply_exact_owner_facts_and_missing_role_diagnostics(self):
        self.require_postgresql()
        objects, entries = self.seed_distinct_local(3)
        MediaObject.objects.update(upload_owner=None, reference_inventory_complete=False)
        JournalMediaReference.objects.all()._raw_delete("default")
        JournalEntry.objects.update(poster_file="")
        UserSettings.objects.filter(user=self.owner).update(avatar=objects[0].reference_name)
        Column.objects.create(author=self.other, title="Other owner cover", cover=objects[1].reference_name)
        SiteSettings.objects.filter(pk=1).update(site_avatar="media-objects/" + str(uuid.uuid4()))
        report = self.assert_fresh_equivalent()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertTrue(any(row["issue"] == "MISSING_MEDIA_ROW" for row in report["other_roles"]))
        self.assertEqual(next(row for row in report["slots"] if row["entry_id"] == entries[1].pk)["issue"], "LINK_ONLY")

    def test_inventory_uses_read_only_sql_preserves_files_and_never_calls_remote_adapter(self):
        self.require_postgresql()
        objects, _entries = self.seed_distinct_local(2)
        from .test_media_storage import r2_backend

        remote = r2_backend("context-remote", 1)
        MediaObject.objects.filter(pk=objects[1].pk).update(storage_backend=remote)
        root = Path(settings.MEDIA_LOCAL_STORAGE_ROOT)
        before = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}
        before_rows = list(MediaObject.objects.values())
        with patch.object(StoragePoolService, "adapter_for", side_effect=AssertionError("No remote adapter in inventory")), \
                patch.object(DynamicLocalBackend, "write", side_effect=AssertionError("Read-only files")), \
                patch.object(DynamicLocalBackend, "delete", side_effect=AssertionError("Read-only files")), \
                CaptureQueriesContext(connection) as queries:
            report = inspect_media_references()
        self.assertEqual(list(MediaObject.objects.values()), before_rows)
        self.assertEqual({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}, before)
        for row in queries:
            self.assertNotIn(row["sql"].lstrip().split(None, 1)[0].upper(), {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE"})
        self.assertTrue(any(row["byte_verification"] == "UNVERIFIED_REMOTE_BYTES" for row in report["objects"]))
        self.assertEqual(report, self.fresh_report())

    def test_database_actually_rejects_writes_in_the_inventory_snapshot(self):
        self.require_postgresql()
        self.seed_distinct_local(1)
        def attempted_write(media):
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')")
                self.assertEqual(cursor.fetchone(), ("repeatable read", "on"))
            MediaObject.objects.filter(pk=media.pk).update(inventory_error="should-never-write")
        with patch("journal.media_inventory._bytes_issue", side_effect=attempted_write):
            with self.assertRaises(DatabaseError):
                inspect_media_references()
        self.assertFalse(MediaObject.objects.filter(inventory_error="should-never-write").exists())

    def test_outer_transactions_keep_live_path_and_existing_isolation(self):
        self.require_postgresql()
        self.seed_distinct_local(1)
        with transaction.atomic(), patch("journal.media_inventory_context.InventoryReadContext", side_effect=AssertionError("No context in caller transaction")):
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                before = cursor.fetchone()
            self.assertEqual(inspect_media_references(), self.fresh_report())
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                self.assertEqual(cursor.fetchone(), before)

    def test_non_postgresql_path_never_constructs_a_context(self):
        self.seed_distinct_local(1)
        with patch.object(connection, "vendor", "sqlite"), \
                patch("journal.media_inventory_context.InventoryReadContext", side_effect=AssertionError("No PG context")):
            self.assertEqual(inspect_media_references(), self.fresh_report())

    def test_manual_transaction_keeps_live_path_without_changing_isolation(self):
        self.require_postgresql()
        self.seed_distinct_local(1)
        connection.set_autocommit(False)
        try:
            with patch("journal.media_inventory_context.InventoryReadContext", side_effect=AssertionError("No context in manual transaction")):
                with connection.cursor() as cursor:
                    cursor.execute("SHOW transaction_isolation")
                    before = cursor.fetchone()
                self.assertEqual(inspect_media_references(), self.fresh_report())
                with connection.cursor() as cursor:
                    cursor.execute("SHOW transaction_isolation")
                    self.assertEqual(cursor.fetchone(), before)
        finally:
            connection.rollback()
            connection.set_autocommit(True)

    def test_missing_domains_and_expired_context_fail_explicitly(self):
        self.require_postgresql()
        self.seed_distinct_local(1)
        from .media_inventory import expected_holding
        from .media_inventory_context import InventoryReadContext, ReadContextIncomplete

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            context = InventoryReadContext()
            entry = next(context.entries())
            with self.assertRaises(ReadContextIncomplete):
                expected_holding(entry, "poster_file", {}, read_source=context)
            with self.assertRaises(ReadContextIncomplete):
                context.owners_for(MediaObject(pk=uuid.uuid4()))
            self.assertIsNone(context.media_for_id(uuid.uuid4()))
            context.close()
            with self.assertRaises(ReadContextIncomplete):
                context.resolve_candidates(entry.custom_poster_url)

    def test_concurrent_commit_is_excluded_from_snapshot_and_seen_by_next_live_call(self):
        self.require_postgresql()
        objects, entries = self.seed_distinct_local(1)
        from .media_inventory import _bytes_issue, expected_holding
        from .media_references import poster_usage

        failures, commits = [], []
        def writer():
            try:
                with transaction.atomic():
                    JournalMediaReference.objects.filter(entry=entries[0], slot="custom_poster_url").update(owner=self.other)
                    MediaObject.objects.filter(pk=objects[0].pk).update(sha256="")
                commits.append(True)
            except BaseException as error:
                failures.append(error)
            finally:
                connection.close()
        def concurrent_bytes_check(media):
            task = threading.Thread(target=writer)
            task.start()
            task.join(timeout=10)
            self.assertFalse(task.is_alive())
            self.assertEqual(failures, [])
            return _bytes_issue(media)
        with patch("journal.media_inventory._bytes_issue", side_effect=concurrent_bytes_check):
            report = inspect_media_references()
        self.assertEqual(commits, [True])
        self.assertEqual(report["status"], "READY")
        with patch("journal.media_inventory_context.InventoryReadContext", side_effect=AssertionError("Live authorization must not use inventory context")):
            self.assertEqual(poster_usage(self.owner.pk).status, "UNKNOWN_USAGE")
            entries[0].refresh_from_db()
            self.assertEqual(expected_holding(entries[0], "custom_poster_url")[1], "OWNER_CONFLICT")
        self.assertEqual(self.assert_fresh_equivalent()["status"], "BLOCKED")

    def test_incomplete_context_blocks_without_inventing_empty_facts(self):
        self.require_postgresql()
        self.seed_distinct_local(1)
        from .media_inventory_context import ReadContextIncomplete

        expected = self.fresh_report()
        with patch("journal.media_inventory_context.InventoryReadContext.owners_for", side_effect=ReadContextIncomplete("Owner domain missing")):
            actual = inspect_media_references()
        expected["status"] = "BLOCKED"
        self.assertEqual(actual, expected)

    def test_runtime_configuration_change_blocks_and_next_invocation_refreshes(self):
        self.require_postgresql()
        objects, _entries = self.seed_distinct_local(1)
        from .media_inventory import _bytes_issue

        def change_configuration(media):
            settings.MEDIA_HISTORICAL_PUBLIC_BASES[str(self.backend.pk)] = ["https://changed.example.test/files"]
            return _bytes_issue(media)
        with override_settings(MEDIA_HISTORICAL_PUBLIC_BASES={}):
            with patch("journal.media_inventory._bytes_issue", side_effect=change_configuration):
                actual = inspect_media_references()
            self.assertEqual(actual["status"], "BLOCKED")
            self.assertEqual(len(actual["objects"]), 1)
            self.assertEqual(self.assert_fresh_equivalent()["status"], "READY")
        MediaObject.objects.filter(pk=objects[0].pk).update(sha256="")
        self.assertEqual(self.assert_fresh_equivalent()["status"], "BLOCKED")
