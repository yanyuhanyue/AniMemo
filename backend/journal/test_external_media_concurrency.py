from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from journal.external_media.errors import ExternalMediaError
from journal.external_media.services import bind_external_identity
from journal.models import ExternalMediaIdentity, JournalEntry


def normalized_subject(external_id):
    return {
        "title": "并发测试",
        "japanese_title": "Concurrency",
        "summary": "",
        "episodes": 12,
        "air_date": "2026-01-01",
        "studio": "Test Studio",
        "tags": [],
        "score": 8.0,
        "poster_url": "",
        "thumbnail_url": "",
        "provider_name": "Bangumi",
        "provider_url": f"https://bgm.tv/subject/{external_id}",
        "external_id": external_id,
    }


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row-level locking")
class ExternalMediaIdentityPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="identity-race", password="StrongPass123!")
        self.entries = [
            JournalEntry.objects.create(user=self.user, title="并发记录 A"),
            JournalEntry.objects.create(user=self.user, title="并发记录 B"),
        ]

    def test_same_user_subject_can_only_be_bound_once_under_race(self):
        barrier = Barrier(2)

        def fetch_subject(_provider, external_id, *, force=False):
            barrier.wait(timeout=10)
            return normalized_subject(str(external_id))

        def bind(entry_id):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                entry = JournalEntry.objects.get(pk=entry_id)
                identity = bind_external_identity(
                    entry=entry,
                    user=user,
                    provider_slug="bangumi",
                    external_id="424242",
                )
                return "created", identity.entry_id
            except ExternalMediaError as error:
                return error.detail["code"], error.detail.get("entry_id")
            finally:
                connections.close_all()

        with patch(
            "journal.external_media.providers.bangumi.BangumiProvider.fetch_subject",
            autospec=True,
            side_effect=fetch_subject,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(bind, [entry.pk for entry in self.entries]))

        self.assertEqual(sorted(result[0] for result in results), ["created", "subject_already_bound"])
        identities = ExternalMediaIdentity.objects.filter(
            entry__user=self.user,
            provider="bangumi",
            external_id="424242",
        )
        self.assertEqual(identities.count(), 1)
        conflict = next(result for result in results if result[0] == "subject_already_bound")
        self.assertEqual(conflict[1], identities.get().entry_id)
