from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from journal.domain_services import JournalEntryService, JournalEntryServiceError
from journal.models import JournalEntry
from journal.serializers_entries import JournalEntrySerializer


class JournalEntryServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("service-user", password="password-123")
        self.other = get_user_model().objects.create_user("service-other", password="password-123")
        self.service = JournalEntryService(self.user)

    def test_list_and_get_are_owner_scoped_and_return_shared_dto(self):
        owned = JournalEntry.objects.create(user=self.user, title="Owned")
        JournalEntry.objects.create(user=self.other, title="Foreign")

        self.assertEqual([row["entry_id"] for row in self.service.list()], [owned.pk])
        self.assertEqual(self.service.get(owned.pk)["title"], "Owned")
        with self.assertRaisesRegex(JournalEntryServiceError, "不存在"):
            self.service.get(JournalEntry.objects.filter(user=self.other).values_list("pk", flat=True).get())

    def test_plugin_mutations_share_serializer_allowlist_and_hooks(self):
        with patch("journal.domain_services.run_hook") as run_hook:
            created = self.service.create_from_fields(
                {"title": "Created", "watch_status": "completed"},
                serializer_class=JournalEntrySerializer,
                source="plugin",
            )
            updated = self.service.update_from_fields(
                created["entry_id"],
                {"review": "Updated"},
                serializer_class=JournalEntrySerializer,
                source="plugin",
            )

        self.assertEqual(updated["review"], "Updated")
        self.assertEqual([call.args[0] for call in run_hook.call_args_list], ["journal.after_create", "journal.after_update"])
        self.assertEqual([call.args[1].source for call in run_hook.call_args_list], ["plugin", "plugin"])
        with self.assertRaises(JournalEntryServiceError) as caught:
            self.service.create_from_fields(
                {"title": "Rejected", "user_id": self.other.pk},
                serializer_class=JournalEntrySerializer,
                source="plugin",
            )
        self.assertEqual(caught.exception.code, "invalid_entry")

    def test_update_rejects_foreign_and_deleted_entries(self):
        foreign = JournalEntry.objects.create(user=self.other, title="Foreign")
        with self.assertRaises(JournalEntryServiceError) as caught:
            self.service.update_from_fields(foreign.pk, {"review": "x"}, serializer_class=JournalEntrySerializer)
        self.assertEqual(caught.exception.code, "entry_not_found")

        deleted = JournalEntry.objects.create(
            user=self.user,
            title="Deleted",
            deleted_at=timezone.now() - timedelta(days=1),
        )
        with self.assertRaises(JournalEntryServiceError) as caught:
            self.service.get(deleted.pk)
        self.assertEqual(caught.exception.code, "entry_not_found")
