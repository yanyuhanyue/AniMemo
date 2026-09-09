"""Resource regressions that retain per-slot and soft-deleted usage semantics."""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .media_references import poster_usage
from .models import JournalEntry, JournalMediaReference
from .test_media_references import MediaReferenceFixture


class MediaUsageProjectionTests(MediaReferenceFixture):
    def test_usage_skips_large_unrelated_fields_across_multiple_batches(self):
        _source, media, _url = self.upload()
        entries = JournalEntry.objects.bulk_create([
            JournalEntry(user=self.owner, title=f"usage-{number}", poster_file=media.reference_name,
                         review="与封面计费无关的长短评" * 1000, description="完整简介" * 1000,
                         deleted_at=timezone.now() if number % 4 == 0 else None)
            for number in range(257)
        ])
        JournalMediaReference.objects.bulk_create([
            JournalMediaReference(owner=self.owner, entry=entry, media=media, slot="poster_file", value=media.reference_name)
            for entry in entries
        ])
        with CaptureQueriesContext(connection) as queries:
            usage = poster_usage(self.owner.pk)
        self.assertEqual(usage.known_bytes, media.size_bytes * 258)
        self.assertEqual(usage.unknown_slots, ())
        reads = [row["sql"] for row in queries if '"journal_journalentry"' in row["sql"]]
        self.assertTrue(reads)
        for sql in reads:
            self.assertNotIn('"review"', sql)
            self.assertNotIn('"description"', sql)
            self.assertNotIn('"tags"', sql)
