"""Partial recovery must keep cleanup closed and preserve transaction rollback."""

import io
import json
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import post_save, pre_save
from django.test.utils import CaptureQueriesContext
from site_config.media_storage.local import DynamicLocalBackend
from site_config.models import MediaObject

from .media_inventory import (
    _backfill_entry_batch,
    _insert_backfill_holding,
    backfill_media_references,
)
from .models import JournalEntry, JournalMediaReference
from .test_media_references import MediaReferenceFixture


class MediaBackfillBatchTests(MediaReferenceFixture):
    def legacy_pair(self):
        first, first_media, _ = self.upload("First legacy record")
        second, second_media, _ = self.upload("Second legacy record")
        JournalMediaReference.objects.all().delete()
        return first, second, first_media, second_media

    def test_partial_batch_keeps_all_cleanup_gates_closed_until_final_proof(self):
        first, second, first_media, second_media = self.legacy_pair()
        before = list(JournalEntry.objects.order_by("pk").values())
        partial = backfill_media_references(limit=1)
        self.assertEqual(partial["status"], "INCOMPLETE")
        self.assertEqual(partial["next_after_entry"], first.pk)
        self.assertEqual(len(partial["slots"]), 2)
        self.assertEqual(partial["usage"][0]["known_bytes"], first_media.size_bytes + second_media.size_bytes)
        self.assertFalse(MediaObject.objects.filter(reference_inventory_complete=True).exists())
        self.assertEqual(set(MediaObject.objects.values_list("inventory_error", flat=True)), {"BACKFILL_INCOMPLETE"})
        pending = next(row for row in partial["objects"] if row["media_id"] == str(second_media.pk))
        self.assertIn("REFERENCE_MISSING", pending["issues"])
        final = backfill_media_references(after_entry=first.pk, limit=1)
        self.assertEqual(final["status"], "READY", final)
        self.assertFalse(final["more"])
        self.assertEqual(final["next_after_entry"], second.pk)
        self.assertEqual(JournalMediaReference.objects.count(), 2)
        self.assertFalse(MediaObject.objects.filter(reference_inventory_complete=False).exists())
        self.assertEqual(list(JournalEntry.objects.order_by("pk").values()), before)

    def test_partial_batch_does_not_erase_committed_deletion_proof(self):
        entry, media, _ = self.upload()
        with patch("journal.media_references.cleanup_media", return_value=False):
            entry.delete()
        MediaObject.objects.filter(pk=media.pk).update(
            lifecycle=MediaObject.Lifecycle.DELETING, reference_inventory_complete=True,
            inventory_error="deletion-proof-retained",
        )
        # A committed deletion claim may outlive its process. The bytes remain
        # here so the test can detect an unauthorized cleanup or overwrite.
        path = DynamicLocalBackend(self.backend).path_for(media.object_key)
        before = path.read_bytes()
        JournalEntry.objects.bulk_create([
            JournalEntry(user=self.owner, title="Pending one"),
            JournalEntry(user=self.owner, title="Pending two"),
        ])
        result = backfill_media_references(limit=1)
        self.assertEqual(result["status"], "INCOMPLETE")
        media.refresh_from_db()
        self.assertEqual(media.lifecycle, MediaObject.Lifecycle.DELETING)
        self.assertTrue(media.reference_inventory_complete)
        self.assertEqual(media.inventory_error, "deletion-proof-retained")
        self.assertEqual(path.read_bytes(), before)

    def test_outer_rollback_reverts_partial_associations_and_gate_updates(self):
        self.legacy_pair()
        before_entries = list(JournalEntry.objects.order_by("pk").values())
        before_media = list(MediaObject.objects.order_by("pk").values())
        with self.assertRaisesRegex(RuntimeError, "outer rollback"), transaction.atomic():
            result = backfill_media_references(limit=1)
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(JournalMediaReference.objects.count(), 1)
            raise RuntimeError("outer rollback")
        self.assertFalse(JournalMediaReference.objects.exists())
        self.assertEqual(list(JournalEntry.objects.order_by("pk").values()), before_entries)
        self.assertEqual(list(MediaObject.objects.order_by("pk").values()), before_media)

    def test_all_batches_preserves_report_with_bounded_full_inventory_work(self):
        self.legacy_pair()
        JournalEntry.objects.bulk_create([
            JournalEntry(user=self.owner, title=f"No poster {number}") for number in range(4)
        ])
        expected = backfill_media_references(limit=1)
        while expected["more"]:
            expected = backfill_media_references(after_entry=expected["next_after_entry"], limit=1)
        self.assertEqual(expected["status"], "READY")
        JournalMediaReference.objects.all().delete()
        MediaObject.objects.update(reference_inventory_complete=False, inventory_error="")
        output = io.StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("reconcile_media_references", apply=True, all_batches=True, limit=1, stdout=output)
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(JournalMediaReference.objects.count(), 2)
        # Six entry batches must not produce six full database inventories.
        snapshots = [row for row in queries if row["sql"].startswith(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")]
        if connection.vendor == "postgresql":
            self.assertGreater(len(snapshots), 0)
        self.assertLessEqual(len(snapshots), 2)

    def test_all_batches_interruption_keeps_closed_gates_and_can_resume(self):
        first, second, _first_media, _second_media = self.legacy_pair()
        third, _media, _url = self.upload("Third legacy record")
        JournalMediaReference.objects.all().delete()
        before_entries = list(JournalEntry.objects.order_by("pk").values())

        def fail_after_second(*, after_entry, limit):
            result = _backfill_entry_batch(after_entry, limit)
            if result["next_after_entry"] == second.pk:
                raise RuntimeError("interrupted middle batch")
            return result

        with patch("journal.media_inventory._backfill_entry_batch", side_effect=fail_after_second), \
                self.assertRaisesRegex(RuntimeError, "interrupted middle batch"):
            call_command("reconcile_media_references", apply=True, all_batches=True, limit=1, stdout=io.StringIO())
        self.assertEqual(set(JournalMediaReference.objects.values_list("entry_id", flat=True)), {first.pk, second.pk})
        self.assertFalse(MediaObject.objects.filter(reference_inventory_complete=True).exists())
        output = io.StringIO()
        call_command("reconcile_media_references", apply=True, all_batches=True, limit=1, stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["next_after_entry"], third.pk)
        self.assertEqual(JournalMediaReference.objects.count(), 3)
        self.assertEqual(list(JournalEntry.objects.order_by("pk").values()), before_entries)

    def test_all_batches_inside_outer_transaction_rolls_back(self):
        self.legacy_pair()
        before_media = list(MediaObject.objects.order_by("pk").values())
        with self.assertRaisesRegex(RuntimeError, "outer all-batch rollback"), transaction.atomic():
            output = io.StringIO()
            call_command("reconcile_media_references", apply=True, all_batches=True, limit=1, stdout=output)
            self.assertEqual(json.loads(output.getvalue())["status"], "READY")
            self.assertEqual(JournalMediaReference.objects.count(), 2)
            raise RuntimeError("outer all-batch rollback")
        self.assertFalse(JournalMediaReference.objects.exists())
        self.assertEqual(list(MediaObject.objects.order_by("pk").values()), before_media)

    def test_damaged_bytes_keep_direct_holding_but_do_not_establish_url_holding(self):
        entry, media, url = self.upload()
        JournalEntry.objects.filter(pk=entry.pk).update(custom_poster_url=url)
        JournalMediaReference.objects.all().delete()
        MediaObject.objects.filter(pk=media.pk).update(sha256="0" * 64, reference_inventory_complete=False)
        path = DynamicLocalBackend(self.backend).path_for(media.object_key)
        original_bytes = path.read_bytes()
        result = backfill_media_references()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(list(entry.media_references.values_list("slot", "media_id", "value")),
                         [("poster_file", media.pk, media.reference_name)])
        media.refresh_from_db()
        self.assertFalse(media.reference_inventory_complete)
        self.assertIn("MISSING_OR_INVALID_BYTES", result["objects"][0]["issues"])
        self.assertEqual(path.read_bytes(), original_bytes)
        entry.refresh_from_db()
        self.assertEqual(entry.custom_poster_url, url)

    def test_existing_mismatched_holding_is_reported_without_overwrite(self):
        entry, media, _url = self.upload()
        ref = entry.media_references.get()
        JournalMediaReference.objects.filter(pk=ref.pk).update(value="existing mismatched value")
        before = JournalMediaReference.objects.get(pk=ref.pk)
        result = backfill_media_references()
        after = JournalMediaReference.objects.get(pk=ref.pk)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual((after.entry_id, after.owner_id, after.media_id, after.slot, after.value, after.created_at),
                         (before.entry_id, before.owner_id, before.media_id, before.slot, before.value, before.created_at))
        self.assertEqual(JournalMediaReference.objects.count(), 1)
        self.assertIn("REFERENCE_MISMATCH", result["objects"][0]["issues"])
        media.refresh_from_db()
        self.assertFalse(media.reference_inventory_complete)

    def test_backfill_preserves_registered_reference_save_hooks(self):
        entry, _media, _url = self.upload()
        JournalMediaReference.objects.all().delete()
        calls = []

        def before_save(sender, instance, **_kwargs):
            calls.append("before")
            instance.value = "hook-provided-value"

        def after_save(sender, instance, created, **_kwargs):
            calls.append(("after", created, instance.pk))

        pre_save.connect(before_save, sender=JournalMediaReference, weak=False)
        post_save.connect(after_save, sender=JournalMediaReference, weak=False)
        try:
            _backfill_entry_batch(0, 200)
            ref = entry.media_references.get()
            self.assertEqual(ref.value, "hook-provided-value")
            self.assertEqual(calls, ["before", ("after", True, ref.pk)])
            _backfill_entry_batch(0, 200)
            self.assertEqual(calls, ["before", ("after", True, ref.pk)])
            self.assertEqual(entry.media_references.count(), 1)
        finally:
            pre_save.disconnect(before_save, sender=JournalMediaReference)
            post_save.disconnect(after_save, sender=JournalMediaReference)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL named-conflict constraint enforcement")
    def test_backfill_insert_does_not_suppress_check_or_foreign_key_failures(self):
        entry, media, _url = self.upload()
        before = list(JournalMediaReference.objects.values())
        with self.assertRaises(IntegrityError), transaction.atomic():
            _insert_backfill_holding(entry, "invalid-slot", self.owner.pk, media, media.reference_name)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _insert_backfill_holding(entry, "custom_poster_url", -123, media, "https://example.test/poster")
            connection.check_constraints()
        self.assertEqual(list(JournalMediaReference.objects.values()), before)
