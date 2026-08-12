import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from performance.contract import DATASETS, has_query_scaling_regression
from performance.probe import (
    EXPECTED_STATUS_CODES,
    duplicate_query_summary,
    normalize_sql,
)
from performance.seed import provision_load_user_journeys, seed_backend_performance_data
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from journal.models import ExternalMediaIdentity, JournalEntry, WatchHistoryRecord


class PerformanceMeasurementContractTests(TestCase):
    def test_dataset_sizes_match_shared_contract(self):
        self.assertEqual(DATASETS["small"].journal_entries, 50)
        self.assertEqual(DATASETS["medium"].journal_entries, 1_000)
        self.assertEqual(DATASETS["large"].journal_entries, 10_000)
        self.assertEqual(DATASETS["large"].watch_history_records, 5_000)

    def test_query_scaling_guard_is_red_capable(self):
        self.assertFalse(has_query_scaling_regression(3, 7, tolerance=5))
        self.assertTrue(has_query_scaling_regression(3, 9, tolerance=5))

    def test_duplicate_query_normalization_ignores_literal_values(self):
        first = "SELECT * FROM table_name WHERE id = 1 AND name = 'alpha'"
        second = "SELECT * FROM table_name WHERE id = 99 AND name = 'beta'"
        self.assertEqual(normalize_sql(first), normalize_sql(second))
        summary = duplicate_query_summary([{"sql": first}, {"sql": second}])
        self.assertEqual(summary["duplicate_executions"], 1)

    def test_probe_success_contract_rejects_error_pages(self):
        self.assertEqual(EXPECTED_STATUS_CODES, {200})
        self.assertNotIn(400, EXPECTED_STATUS_CODES)

    def test_authoritative_command_refuses_sqlite(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite refusal is exercised only on the local auxiliary database")
        with self.assertRaisesRegex(CommandError, "requires PostgreSQL"):
            call_command(
                "benchmark_backend_performance",
                dataset="small",
                output="ignored.json",
            )

    def test_small_seed_is_exact_and_repeatable(self):
        first = seed_backend_performance_data("small")
        second = seed_backend_performance_data("small")
        self.assertEqual(first.dataset, "SMALL")
        self.assertEqual(second.journal_entries, 50)
        self.assertEqual(second.supporting_users, 10)
        self.assertEqual(second.plugins, 5)
        self.assertEqual(second.watch_history_records, 25)
        self.assertEqual(
            JournalEntry.objects.filter(user_id=second.owner_id, deleted_at__isnull=True).count(),
            50,
        )

    def test_load_user_journeys_are_distinct_owned_read_fixtures(self):
        seed_backend_performance_data("small")

        identities = provision_load_user_journeys(5)

        self.assertEqual(len(identities), 5)
        self.assertEqual(len({identity.username for identity in identities}), 5)
        self.assertEqual(len({identity.entry_id for identity in identities}), 5)
        for identity in identities:
            entry = JournalEntry.objects.get(pk=identity.entry_id)
            self.assertEqual(entry.user.username, identity.username)
            self.assertIn("anime", entry.title.lower())
            self.assertTrue(WatchHistoryRecord.objects.filter(entry=entry).exists())

    def test_load_identity_command_requires_isolation_confirmation(self):
        with self.assertRaisesRegex(CommandError, "confirm-isolated"):
            call_command("provision_performance_load_identities", count=5, stdout=io.StringIO())

    def test_load_identity_command_emits_unique_owned_tokens(self):
        output = io.StringIO()

        call_command(
            "provision_performance_load_identities",
            dataset="small",
            count=5,
            token_minutes=40,
            confirm_isolated=True,
            stdout=output,
        )

        payload = json.loads(output.getvalue())
        identities = payload["identities"]
        self.assertEqual(len(identities), 5)
        self.assertEqual(len({item["username"] for item in identities}), 5)
        self.assertEqual(len({item["entry_id"] for item in identities}), 5)
        self.assertEqual(len({item["access_token"] for item in identities}), 5)
        for item in identities:
            token = AccessToken(item["access_token"])
            self.assertIn("sv", token)
            self.assertGreaterEqual(int(token["exp"]) - int(token["iat"]), 40 * 60)
            entry = JournalEntry.objects.get(pk=item["entry_id"])
            self.assertEqual(entry.user.username, item["username"])


class JournalPerformanceQueryGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("perf-query-owner", password="StrongPass123!")
        entries = [
            JournalEntry(
                user=cls.user,
                title=f"Query Guard {index:03d}",
                airing_period=f"{2000 + index % 20}-01",
                tags=["bounded", f"bucket-{index % 5}"],
                personal_score="8.00",
            )
            for index in range(120)
        ]
        JournalEntry.objects.bulk_create(entries, batch_size=120)
        rows = list(JournalEntry.objects.filter(user=cls.user).order_by("id"))
        ExternalMediaIdentity.objects.bulk_create(
            [
                ExternalMediaIdentity(
                    entry=row,
                    provider="bangumi",
                    external_id=f"guard-{row.pk}",
                    canonical_url=f"https://bgm.tv/subject/{row.pk}",
                    metadata={"title": row.title, "score": 8.0},
                )
                for row in rows
            ],
            batch_size=120,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _query_count(self, page_size):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("entry-list"), {"page_size": page_size})
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_journal_list_query_count_does_not_scale_with_serialized_rows(self):
        small_count = self._query_count(12)
        large_count = self._query_count(120)
        self.assertFalse(
            has_query_scaling_regression(small_count, large_count, tolerance=2),
            f"journal list query count scaled from {small_count} to {large_count}",
        )
