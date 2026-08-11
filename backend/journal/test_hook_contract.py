from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import PendingRegistration
from plugin_host.hook_contract import HOOK_DEFINITIONS
from .account_security import AccountDeletionError, delete_current_account
from .models import Column, JournalEntry
from .registration import complete_registration, request_pending_registration, token_digest


User = get_user_model()


class SupportedHookEmitterTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="hook-owner", password="StrongPass123!")
        self.client.force_authenticate(self.user)

    def test_hook_contract_contains_only_real_emitters(self):
        self.assertEqual(set(HOOK_DEFINITIONS), {
            "registration.before_request", "registration.before_complete", "registration.after_complete",
            "journal.after_create", "journal.after_update", "journal.after_delete",
            "column.after_publish", "column.after_delete",
            "user.after_created", "user.before_delete", "user.after_delete",
        })
        self.assertEqual(HOOK_DEFINITIONS["user.before_delete"], {"mode": "filter", "failure": "closed", "scope": "system"})
        self.assertEqual(HOOK_DEFINITIONS["user.after_delete"], {"mode": "action", "failure": "open", "scope": "system"})

    def test_registration_hooks_and_user_after_created_run_at_completion(self):
        request = RequestFactory().post("/api/auth/register/complete/")
        with patch("journal.registration.run_registration_hook") as registration_hook:
            request_pending_registration(request=request, email="hook-register@example.com")
        registration_hook.assert_called_once()
        self.assertEqual(registration_hook.call_args.args[0], "registration.before_request")

        completion_token = "hook-completion-token"
        PendingRegistration.objects.filter(email="hook-register@example.com").update(
            verified_at=timezone.now(),
            completion_token_hash=token_digest(completion_token),
            completion_token_expires_at=timezone.now() + timedelta(minutes=10),
        )
        with patch("journal.registration.run_registration_hook") as registration_hook, patch("journal.registration.run_hook") as user_hook:
            user, error = complete_registration(
                request=request,
                completion_token=completion_token,
                username="hook-registered-user",
                password="StrongPass123!",
            )
        self.assertIsNone(error)
        self.assertEqual([call.args[0] for call in registration_hook.call_args_list], ["registration.before_complete", "registration.after_complete"])
        user_hook.assert_called_once()
        self.assertEqual(user_hook.call_args.args[0], "user.after_created")
        self.assertEqual(user_hook.call_args.args[1].user_id, user.pk)

    def test_journal_crud_hooks_run_from_real_api_flow(self):
        with patch("journal.domain_services.publish_event") as mutation_hook:
            created = self.client.post(reverse("entry-list"), {"title": "Hook Entry"}, format="json")
            self.assertEqual(created.status_code, 201)
            entry_id = created.data["id"]
            updated = self.client.patch(reverse("entry-detail", kwargs={"pk": entry_id}), {"review": "updated"}, format="json")
            self.assertEqual(updated.status_code, 200)
            deleted = self.client.delete(reverse("entry-detail", kwargs={"pk": entry_id}))
            self.assertEqual(deleted.status_code, 204)
        self.assertEqual([call.args[0] for call in mutation_hook.call_args_list], [
            "journal.after_create", "journal.after_update", "journal.after_delete",
        ])

    def test_column_publish_fires_only_on_transition_and_delete_fires_after_delete(self):
        with patch("journal.entry_views.run_hook") as api_hook:
            created = self.client.post(reverse("column-list"), {"title": "Hook Column", "body": "Body"}, format="json")
        self.assertEqual(created.status_code, 201)
        self.assertNotIn("column.after_publish", [call.args[0] for call in api_hook.call_args_list])
        column = Column.objects.get(pk=created.data["id"])

        reviewer = User.objects.create_superuser(username="hook-reviewer", password="StrongPass123!")
        reviewer_client = APIClient()
        reviewer_client.force_authenticate(reviewer)
        with patch("journal.staff_dashboard_views.run_hook") as review_hook:
            first = reviewer_client.patch(reverse("staff-column-review", kwargs={"pk": column.pk}), {"status": Column.Status.APPROVED}, format="json")
            second = reviewer_client.patch(reverse("staff-column-review", kwargs={"pk": column.pk}), {"status": Column.Status.APPROVED}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual([call.args[0] for call in review_hook.call_args_list], ["column.after_publish"])

        with patch("journal.entry_views.run_hook") as delete_hook:
            deleted = self.client.delete(reverse("column-detail", kwargs={"pk": column.pk}))
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual([call.args[0] for call in delete_hook.call_args_list], ["column.after_delete"])

    def test_staff_recycle_and_restore_do_not_emit_delete_hooks(self):
        reviewer = User.objects.create_superuser(username="hook-recycle-reviewer", password="StrongPass123!")
        reviewer_client = APIClient()
        reviewer_client.force_authenticate(reviewer)
        column = Column.objects.create(author=self.user, title="Recycle Column", body="Body")
        entry = JournalEntry.objects.create(user=self.user, title="Recycle Entry")

        with patch("journal.staff_moderation_views.run_hook") as hook:
            recycled = reviewer_client.post(
                reverse("staff-bulk-action", kwargs={"kind": "columns"}),
                {"ids": [column.pk], "action": "recycle", "reason": "cleanup"},
                format="json",
            )
            restored = reviewer_client.post(
                reverse("staff-bulk-action", kwargs={"kind": "columns"}),
                {"ids": [column.pk], "action": "restore"},
                format="json",
            )
            entry_recycled = reviewer_client.post(
                reverse("staff-bulk-action", kwargs={"kind": "entries"}),
                {"ids": [entry.pk], "action": "recycle", "reason": "cleanup"},
                format="json",
            )
            entry_restored = reviewer_client.post(
                reverse("staff-bulk-action", kwargs={"kind": "entries"}),
                {"ids": [entry.pk], "action": "restore"},
                format="json",
            )

        self.assertEqual(recycled.status_code, 200)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(entry_recycled.status_code, 200)
        self.assertEqual(entry_restored.status_code, 200)
        hook.assert_not_called()


class UserDeleteHookContractTests(TestCase):
    def test_before_delete_denial_is_fail_closed(self):
        user = User.objects.create_user(username="hook-denied", password="StrongPass123!")
        with patch("journal.account_security.run_policy", return_value=False) as before, patch("journal.account_security.publish_event") as after:
            with self.assertRaises(AccountDeletionError) as raised:
                delete_current_account(user=user, current_password="StrongPass123!")
        self.assertEqual(raised.exception.reason, "before_delete_hook_denied")
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        before.assert_called_once()
        after.assert_not_called()

    def test_delete_hooks_receive_safe_context_and_after_delete_is_open(self):
        user = User.objects.create_user(username="hook-allowed", password="StrongPass123!")
        seen = {}

        def allow(_hook_name, value, context):
            seen["before"] = context
            return value

        def after(_hook_name, context):
            seen["after"] = context

        with patch("journal.account_security.run_policy", side_effect=allow), patch("journal.account_security.publish_event", side_effect=after):
            delete_current_account(user=user, current_password="StrongPass123!")
        self.assertEqual(seen["before"].user_id, user.pk)
        self.assertEqual(seen["after"].user_id, user.pk)
        self.assertFalse(hasattr(seen["before"], "password"))
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
