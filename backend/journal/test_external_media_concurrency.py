from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from journal.external_accounts.services import apply_import_preview
from journal.external_media.errors import ExternalMediaError
from journal.external_media.services import (
    PreparedIdentity,
    bind_external_identity,
    refresh_external_identity,
    set_metadata_source,
    unbind_external_identity,
)
from journal.models import ExternalImportSession, ExternalMediaIdentity, JournalEntry


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

    def test_refresh_never_writes_old_subject_metadata_into_rebound_identity(self):
        entry = self.entries[0]
        original = ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="bangumi",
            external_id="123",
            canonical_url="https://bgm.tv/subject/123",
            metadata=normalized_subject("123"),
        )
        refresh_started = Event()
        release_refresh = Event()

        def blocked_refresh(_provider, _identity):
            refresh_started.set()
            if not release_refresh.wait(timeout=10):
                raise TimeoutError("refresh test barrier timed out")
            return {**normalized_subject("123"), "title": "过期的 123"}

        def run_refresh():
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                current_entry = JournalEntry.objects.get(pk=entry.pk)
                refresh_external_identity(entry=current_entry, user=user, provider_slug="bangumi")
                return "updated"
            except ExternalMediaError as error:
                return error.detail["code"]
            finally:
                connections.close_all()

        with patch(
            "journal.external_media.providers.bangumi.BangumiProvider.refresh",
            autospec=True,
            side_effect=blocked_refresh,
        ), patch(
            "journal.external_media.providers.bangumi.BangumiProvider.fetch_subject",
            autospec=True,
            side_effect=lambda _provider, external_id, force=False: normalized_subject(str(external_id)),
        ), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_refresh)
            self.assertTrue(refresh_started.wait(timeout=10))
            unbind_external_identity(entry=entry, user=self.user, provider_slug="bangumi")
            rebound = bind_external_identity(entry=entry, user=self.user, provider_slug="bangumi", external_id="456")
            release_refresh.set()
            result = future.result(timeout=15)

        self.assertEqual(result, "external_identity_changed")
        self.assertFalse(ExternalMediaIdentity.objects.filter(pk=original.pk).exists())
        rebound.refresh_from_db()
        self.assertEqual(rebound.external_id, "456")
        self.assertEqual(rebound.metadata["external_id"], "456")
        self.assertNotEqual(rebound.metadata["title"], "过期的 123")

    def test_simultaneous_imports_create_only_one_same_user_subject(self):
        row = {
            "provider": "bangumi",
            "external_id": "777",
            "title": "并发导入",
            "japanese_title": "Concurrent Import",
            "poster_url": "",
            "remote_status": "planned",
            "remote_status_label": "想看",
            "remote_status_code": 1,
            "remote_rating": None,
            "remote_comment": "",
            "remote_comment_summary": "",
            "remote_tags": [],
            "remote_updated_at": "",
        }
        sessions = [
            ExternalImportSession.objects.create(
                user=self.user,
                provider="bangumi",
                snapshot=[row],
                expires_at=timezone.now() + timedelta(minutes=20),
            )
            for _ in range(2)
        ]
        barrier = Barrier(2)

        def prepare(_provider_slug, external_id):
            barrier.wait(timeout=10)
            return PreparedIdentity(
                provider="bangumi",
                external_id=str(external_id),
                canonical_url=f"https://bgm.tv/subject/{external_id}",
                metadata=normalized_subject(str(external_id)),
                metadata_fetched_at=timezone.now(),
            )

        def apply(session_id):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                return apply_import_preview(
                    user=user,
                    provider_slug="bangumi",
                    preview_id=session_id,
                    items=[{"external_id": "777", "mode": "CREATE_NEW"}],
                )
            finally:
                connections.close_all()

        with patch("journal.external_accounts.imports.prepare_identity", side_effect=prepare), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, [session.pk for session in sessions]))

        statuses = [result["results"][0]["status"] for result in results]
        self.assertEqual(sorted(statuses), ["conflict", "created"])
        self.assertEqual(
            ExternalMediaIdentity.objects.filter(entry__user=self.user, provider="bangumi", external_id="777").count(),
            1,
        )
        self.assertEqual(JournalEntry.objects.filter(user=self.user, title="并发测试").count(), 1)

    def test_metadata_source_switch_keeps_at_most_one_source(self):
        entry = self.entries[0]
        ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="bangumi",
            external_id="1",
            canonical_url="https://bgm.tv/subject/1",
            metadata=normalized_subject("1"),
            is_metadata_source=True,
        )
        ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="anilist",
            external_id="2",
            canonical_url="https://anilist.co/anime/2",
            metadata=normalized_subject("2"),
        )
        barrier = Barrier(2)

        def switch(provider_slug):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                current_entry = JournalEntry.objects.get(pk=entry.pk)
                barrier.wait(timeout=10)
                selected, _applied, _changed = set_metadata_source(
                    entry=current_entry,
                    user=user,
                    provider_slug=provider_slug,
                    apply_metadata=False,
                )
                return selected.provider
            finally:
                connections.close_all()

        with patch(
            "journal.external_media.services.get_provider",
            side_effect=lambda slug: SimpleNamespace(slug=slug),
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(switch, ["bangumi", "anilist"]))

        self.assertEqual(sorted(results), ["anilist", "bangumi"])
        self.assertEqual(
            ExternalMediaIdentity.objects.filter(entry=entry, is_metadata_source=True).count(),
            1,
        )
