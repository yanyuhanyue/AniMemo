import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import management
from django.core.management.base import CommandError
from django.test import TestCase

from .models import JournalEntry


class ProfileJournalQueriesCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="profile-user",
            email="profile-user@example.com",
            password="StrongPass123!",
        )
        JournalEntry.objects.create(user=self.user, title="可分析记录")

    def test_json_profile_is_scoped_and_does_not_leak_identity_fields(self):
        stream = StringIO()
        management.call_command(
            "profile_journal_queries",
            user_id=self.user.pk,
            limit=1,
            format="json",
            stdout=stream,
        )
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["user_id"], self.user.pk)
        self.assertNotIn("username", payload)
        self.assertNotIn("email", payload)
        self.assertNotIn("query_sql", payload)

    def test_text_profile_contains_only_user_id_and_plan(self):
        stream = StringIO()
        management.call_command(
            "profile_journal_queries",
            username=self.user.username,
            limit=1,
            format="text",
            stdout=stream,
        )
        text = stream.getvalue()
        self.assertIn(f"user_id: {self.user.pk}", text)
        self.assertIn("index_change:", text)
        self.assertNotIn(self.user.username, text)
        self.assertNotIn(self.user.email, text)

    def test_scope_is_required_and_limit_is_bounded(self):
        with self.assertRaises(CommandError):
            management.call_command("profile_journal_queries", limit=1)
        with self.assertRaises(CommandError):
            management.call_command("profile_journal_queries", user_id=self.user.pk, limit=501)
