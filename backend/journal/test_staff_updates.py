from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import StaffProfile
from journal.models import AdminAuditLog
from journal.update_agent_client import AgentResponseError, AgentUnavailable, UpdateAgentClient


User = get_user_model()


class UpdateAgentClientTests(APITestCase):
    @patch("journal.update_agent_client.socket.AF_UNIX", None, create=True)
    def test_platform_without_unix_socket_reports_agent_unavailable(self):
        with self.assertRaisesRegex(AgentUnavailable, "requires Unix Socket support"):
            UpdateAgentClient(socket_path="/run/animemo-updater/updater.sock").request("get_status")


class StaffUpdateApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="viewer", password="StrongPass123!")
        self.operator = User.objects.create_user(username="operator", password="StrongPass123!", is_staff=True)
        StaffProfile.objects.create(user=self.operator, role=StaffProfile.Role.OPERATOR)
        self.reviewer = User.objects.create_user(username="reviewer", password="StrongPass123!", is_staff=True)
        StaffProfile.objects.create(user=self.reviewer, role=StaffProfile.Role.REVIEWER)
        self.superuser = User.objects.create_superuser(username="root", email="root@example.com", password="StrongPass123!")

    def csrf_post(self, url, data):
        token = self.client.get(reverse("csrf-token")).data["csrf_token"]
        return self.client.post(url, data, format="json", HTTP_X_CSRFTOKEN=token)

    @patch("journal.staff_update_views._client")
    def test_non_staff_and_staff_without_capability_are_denied(self, client):
        for user in [self.user, self.reviewer]:
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                response = self.client.get(reverse("staff-update-status"))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        client.assert_not_called()

    @patch("journal.staff_update_views._client")
    def test_operator_defaults_to_stable_and_cannot_select_prerelease(self, client):
        client.return_value.request.return_value = {"channel": "stable", "releases": []}
        self.client.force_authenticate(self.operator)

        stable = self.client.get(reverse("staff-update-releases"))
        rc = self.client.get(reverse("staff-update-releases"), {"channel": "rc"})

        self.assertEqual(stable.status_code, status.HTTP_200_OK)
        client.return_value.request.assert_called_once_with("list_releases", {"channel": "stable", "refresh": False})
        self.assertEqual(rc.status_code, status.HTTP_403_FORBIDDEN)

    @patch("journal.staff_update_views._client")
    def test_superuser_can_view_rc_and_beta(self, client):
        client.return_value.request.side_effect = [
            {"channel": "rc", "releases": []},
            {"channel": "beta", "releases": []},
        ]
        self.client.force_authenticate(self.superuser)

        self.assertEqual(self.client.get(reverse("staff-update-releases"), {"channel": "rc"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("staff-update-releases"), {"channel": "beta"}).status_code, 200)

    @patch("journal.staff_update_views._client")
    def test_plan_has_strict_version_dto_and_apply_is_audited(self, client):
        client.return_value.request.side_effect = [
            {
                "planId": "a" * 32,
                "to": {"version": "v1.0.1", "channel": "stable"},
                "compatibility": {"allowed": True},
            },
            {"operation": {"id": "b" * 32, "status": "idle"}},
        ]
        self.client.force_authenticate(self.operator)

        invalid = self.csrf_post(reverse("staff-update-plan"), {"version": "v1.0.1;id"})
        planned = self.csrf_post(reverse("staff-update-plan"), {"version": "v1.0.1"})
        applied = self.csrf_post(reverse("staff-update-apply"), {
            "plan_id": planned.data["planId"],
            "confirmation": "APPLY v1.0.1",
        })

        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(planned.status_code, status.HTTP_200_OK)
        self.assertEqual(applied.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(AdminAuditLog.objects.filter(action="system.update_apply", target_id="v1.0.1").exists())

    @patch("journal.staff_update_views._client")
    def test_update_mutation_without_csrf_is_rejected_before_agent_call(self, client):
        csrf_client = APIClient(enforce_csrf_checks=True)
        self.assertTrue(csrf_client.login(username="operator", password="StrongPass123!"))

        response = csrf_client.post(
            reverse("staff-update-plan"),
            {"version": "v1.0.1"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["code"], "csrf_failed")
        client.assert_not_called()

    @patch("journal.staff_update_views._client")
    def test_agent_errors_keep_machine_readable_contract(self, client):
        client.return_value.request.side_effect = AgentResponseError("当前数据库不兼容", remote_code="incompatible_release")
        self.client.force_authenticate(self.operator)

        response = self.csrf_post(reverse("staff-update-plan"), {"version": "v1.0.1"})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "incompatible_release")
        self.assertEqual(response.data["detail"], "当前数据库不兼容")

    @patch("journal.staff_update_views._client")
    def test_unavailable_agent_is_503_without_leaking_socket_details(self, client):
        client.return_value.request.side_effect = AgentUnavailable("AniMemo Update Agent is unavailable")
        self.client.force_authenticate(self.superuser)

        response = self.client.get(reverse("staff-update-status"))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "updater_unavailable")
        self.assertNotIn("/run/", response.data["detail"])

    @patch("journal.staff_update_views._client")
    def test_rollback_requires_exact_confirmation_and_audits(self, client):
        client.return_value.request.return_value = {"operation": {"id": "c" * 32, "status": "idle"}}
        self.client.force_authenticate(self.operator)

        invalid = self.csrf_post(reverse("staff-update-rollback"), {"confirmation": "yes"})
        accepted = self.csrf_post(reverse("staff-update-rollback"), {"confirmation": "ROLLBACK PREVIOUS"})

        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(accepted.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(AdminAuditLog.objects.filter(action="system.update_rollback").exists())
