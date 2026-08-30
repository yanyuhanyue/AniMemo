import json
from unittest.mock import MagicMock, patch

from accounts.models import StaffProfile
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from journal.models import AdminAuditLog
from journal.update_agent_client import (
    AgentResponseError,
    AgentUnavailable,
    UpdateAgentClient,
    _validated_remote_failure,
)
from journal.update_public_results import (
    UpdateSuccessResultError,
    project_update_success,
)

User = get_user_model()


def _identity(version="v1.0.0"):
    return {
        "version": version,
        "channel": "stable",
        "commit": "1" * 40,
        "apiDigest": "sha256:" + "2" * 64,
        "webDigest": "sha256:" + "3" * 64,
    }


def _compatibility():
    return {
        "allowed": True,
        "decision": "safe_switch",
        "rollbackMode": "safe",
        "migrationRequired": False,
        "migrationPolicy": "none",
        "reasons": [],
    }


def _plan():
    return {
        "planId": "a" * 32,
        "expiresAt": "2026-08-30T12:00:00Z",
        "from": _identity(),
        "to": _identity("v1.0.1"),
        "compatibility": _compatibility(),
        "affectedServices": ["api", "web"],
        "databaseRollback": False,
        "source": "github",
        "transportPolicyIdentity": "4" * 64,
        "verifiedReleaseIdentity": "sha256:" + "5" * 64,
    }


def _operation(identifier="b" * 32, kind="apply_update"):
    return {
        "id": identifier,
        "kind": kind,
        "status": "idle",
        "createdAt": "2026-08-30T12:00:00Z",
        "updatedAt": "2026-08-30T12:00:00Z",
        "events": [
            {
                "status": "idle",
                "at": "2026-08-30T12:00:00Z",
                "detail": "Operation created",
            }
        ],
    }


class UpdateAgentClientTests(APITestCase):
    @patch("journal.update_agent_client.socket.AF_UNIX", None, create=True)
    def test_platform_without_unix_socket_reports_agent_unavailable(self):
        with self.assertRaisesRegex(AgentUnavailable, "requires Unix Socket support"):
            UpdateAgentClient(socket_path="/run/animemo-updater/updater.sock").request("get_status")

    def test_remote_failure_requires_exact_closed_contract(self):
        valid = {
            "code": "incompatible_release",
            "detail": "Release is incompatible with this installation",
            "correlation_id": "a" * 32,
        }
        self.assertEqual(_validated_remote_failure(valid), valid)
        for invalid in (
            {**valid, "extra": "unexpected"},
            {**valid, "code": "ATTACKER_SELECTED_CODE"},
            {**valid, "detail": "UPDATER-REMOTE-STACK-SENTINEL"},
            {**valid, "correlation_id": "not-a-correlation-id"},
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(_validated_remote_failure(invalid))

    @patch("journal.update_agent_client.socket.AF_UNIX", 1, create=True)
    @patch("journal.update_agent_client.socket.socket")
    def test_hostile_ok_success_is_rejected_before_returning_to_django(
        self, socket_factory
    ):
        canaries = {
            "events": [{"detail": "Traceback (most recent call last)"}],
            "path": r"C:\\private\\operator\\runtime.py",
            "sql": "SELECT secret_column FROM internal_table",
        }
        connection = MagicMock()
        connection.recv.return_value = (
            json.dumps({"ok": True, "result": {**_plan(), **canaries}}).encode()
            + b"\n"
        )
        socket_factory.return_value.__enter__.return_value = connection

        with self.assertRaisesRegex(AgentUnavailable, "invalid response") as raised:
            UpdateAgentClient(socket_path="/run/animemo-updater.sock").request(
                "plan_update", {"version": "v1.0.1"}
            )

        exposed = str(raised.exception)
        for canary in canaries.values():
            self.assertNotIn(str(canary), exposed)

    @patch("journal.update_agent_client.socket.AF_UNIX", 1, create=True)
    @patch("journal.update_agent_client.socket.socket")
    def test_request_mismatched_success_is_rejected_by_django_client(
        self, socket_factory
    ):
        hostile = _plan()
        hostile["to"] = _identity("v1.0.2")
        connection = MagicMock()
        connection.recv.return_value = (
            json.dumps({"ok": True, "result": hostile}).encode() + b"\n"
        )
        socket_factory.return_value.__enter__.return_value = connection

        with self.assertRaisesRegex(AgentUnavailable, "invalid response"):
            UpdateAgentClient(socket_path="/run/animemo-updater.sock").request(
                "plan_update", {"version": "v1.0.1"}
            )

    @patch("journal.update_agent_client.socket.AF_UNIX", 1, create=True)
    @patch("journal.update_agent_client.socket.socket")
    def test_replayed_apply_success_is_rejected_by_django_client(
        self, socket_factory
    ):
        connection = MagicMock()
        connection.recv.return_value = (
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "planId": "c" * 32,
                        "operation": _operation(kind="apply_update"),
                    },
                }
            ).encode()
            + b"\n"
        )
        socket_factory.return_value.__enter__.return_value = connection

        with self.assertRaisesRegex(AgentUnavailable, "invalid response"):
            UpdateAgentClient(socket_path="/run/animemo-updater.sock").request(
                "apply_update",
                {"planId": "a" * 32, "confirmation": "APPLY v1.0.1"},
            )

    def test_success_projection_rebuilds_nested_objects(self):
        source = _plan()
        projected = project_update_success(
            "plan_update", source, {"version": "v1.0.1"}
        )

        self.assertEqual(projected, source)
        self.assertIsNot(projected, source)
        self.assertIsNot(projected["compatibility"], source["compatibility"])

    def test_success_projection_rejects_request_mismatches_and_hostile_logs(self):
        requested_id = "b" * 32
        other_id = "c" * 32
        idle = {
            "status": "idle",
            "at": "2026-08-30T12:00:00Z",
            "detail": "Operation created",
        }
        preflight = {
            "status": "preflight",
            "at": "2026-08-30T12:00:01Z",
            "detail": "Preflight checks in progress",
        }
        succeeded = {
            "status": "succeeded",
            "at": "2026-08-30T12:00:02Z",
            "detail": "Update completed",
        }
        cases = (
            (
                "list channel",
                "list_releases",
                {"channel": "beta", "releases": []},
                {"channel": "stable"},
            ),
            (
                "check channel",
                "check_update",
                {
                    "channel": "rc",
                    "currentVersion": "v1.0.0",
                    "latest": {
                        "version": "v1.0.1-rc.1",
                        "channel": "rc",
                        "publishedAt": "2026-08-30T12:00:00Z",
                        "compatibility": _compatibility(),
                    },
                },
                {"channel": "stable"},
            ),
            (
                "plan version",
                "plan_update",
                _plan(),
                {"version": "v1.0.2"},
            ),
            (
                "plan source",
                "plan_update",
                _plan(),
                {"version": "v1.0.1", "source": "official-mirror"},
            ),
            (
                "operation id",
                "get_operation",
                _operation(other_id),
                {"operationId": requested_id},
            ),
            (
                "log operation id",
                "get_logs",
                {"operationId": other_id, "events": [idle]},
                {"operationId": requested_id, "limit": 10},
            ),
            (
                "apply kind",
                "apply_update",
                {
                    "planId": "a" * 32,
                    "operation": _operation(kind="rollback_previous"),
                },
                {"planId": "a" * 32, "confirmation": "APPLY v1.0.1"},
            ),
            (
                "apply plan id",
                "apply_update",
                {
                    "planId": other_id,
                    "operation": _operation(kind="apply_update"),
                },
                {"planId": "a" * 32, "confirmation": "APPLY v1.0.1"},
            ),
            (
                "rollback kind",
                "rollback_previous",
                {"operation": _operation(kind="apply_update")},
                {"confirmation": "ROLLBACK PREVIOUS"},
            ),
            (
                "invalid mutation state",
                "apply_update",
                {
                    "planId": "a" * 32,
                    "operation": {
                        "id": "",
                        "kind": "apply_update",
                        "status": "invalid_operation_state",
                        "createdAt": "",
                        "updatedAt": "",
                        "events": [
                            {
                                "status": "invalid_operation_state",
                                "at": "",
                                "detail": "Operation state is unavailable",
                            }
                        ],
                    },
                },
                {"planId": "a" * 32, "confirmation": "APPLY v1.0.1"},
            ),
            (
                "empty logs",
                "get_logs",
                {"operationId": requested_id, "events": []},
                {"operationId": requested_id, "limit": 10},
            ),
            (
                "invalid-state timestamp",
                "get_logs",
                {
                    "operationId": requested_id,
                    "events": [
                        {
                            "status": "invalid_operation_state",
                            "at": "2026-08-30T12:00:00Z",
                            "detail": "Operation state is unavailable",
                        }
                    ],
                },
                {"operationId": requested_id, "limit": 10},
            ),
            (
                "logs exceed requested limit",
                "get_logs",
                {
                    "operationId": requested_id,
                    "events": [idle, preflight],
                },
                {"operationId": requested_id, "limit": 1},
            ),
            (
                "log time reversal",
                "get_logs",
                {
                    "operationId": requested_id,
                    "events": [
                        preflight,
                        {
                            "status": "fetching",
                            "at": "2026-08-30T12:00:00Z",
                            "detail": "Release acquisition in progress",
                        },
                    ],
                },
                {"operationId": requested_id, "limit": 10},
            ),
            (
                "illegal log transition",
                "get_logs",
                {"operationId": requested_id, "events": [idle, succeeded]},
                {"operationId": requested_id, "limit": 10},
            ),
        )
        for label, operation, result, params in cases:
            with self.subTest(label=label), self.assertRaises(
                UpdateSuccessResultError
            ):
                project_update_success(operation, result, params)

        self.assertEqual(
            project_update_success(
                "get_logs",
                {"operationId": requested_id, "events": [idle, preflight]},
                {"operationId": requested_id, "limit": 2},
            )["events"],
            [idle, preflight],
        )
        self.assertEqual(
            project_update_success(
                "check_update",
                {
                    "channel": "stable",
                    "currentVersion": "v1.0.0",
                    "latest": None,
                },
                {"channel": "stable"},
            )["channel"],
            "stable",
        )


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
            _plan(),
            {"planId": "a" * 32, "operation": _operation()},
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
        audit = AdminAuditLog.objects.get(
            action="system.update_apply", target_id="v1.0.1"
        )
        self.assertEqual(audit.metadata, {"operation_id": "b" * 32})
        plan_audit = AdminAuditLog.objects.get(action="system.update_plan")
        self.assertEqual(plan_audit.after, _plan())

    @patch("journal.staff_update_views._client")
    def test_hostile_success_fails_closed_before_http_or_audit(self, client):
        marker = "Traceback SELECT secret FROM internal_table C:\\private\\runtime.py"
        client.return_value.request.return_value = {
            **_plan(),
            "events": [{"detail": marker}],
        }
        self.client.force_authenticate(self.operator)

        response = self.csrf_post(
            reverse("staff-update-plan"), {"version": "v1.0.1"}
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "updater_unavailable")
        self.assertNotIn(marker, str(response.data))
        self.assertFalse(
            AdminAuditLog.objects.filter(action="system.update_plan").exists()
        )

    @patch("journal.staff_update_views._client")
    def test_request_mismatched_success_fails_closed_before_http_or_audit(
        self, client
    ):
        client.return_value.request.return_value = {
            "channel": "beta",
            "releases": [],
        }
        self.client.force_authenticate(self.operator)

        response = self.client.get(reverse("staff-update-releases"))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "updater_unavailable")
        self.assertFalse(AdminAuditLog.objects.exists())

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
        marker = "UPDATER-REMOTE-STACK-SENTINEL"
        client.return_value.request.side_effect = AgentResponseError(marker, remote_code="incompatible_release")
        self.client.force_authenticate(self.operator)

        response = self.csrf_post(reverse("staff-update-plan"), {"version": "v1.0.1"})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "incompatible_release")
        self.assertIn("correlation_id", response.data)
        self.assertNotIn(marker, str(response.data))

    @patch("journal.staff_update_views._client")
    def test_unknown_agent_error_code_and_detail_fail_closed(self, client):
        marker = "UPDATER-UNKNOWN-STACK-SENTINEL"
        client.return_value.request.side_effect = AgentResponseError(marker, remote_code=marker)
        self.client.force_authenticate(self.operator)

        response = self.csrf_post(reverse("staff-update-plan"), {"version": "v1.0.1"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "updater_request_failed")
        self.assertNotIn(marker, str(response.data))

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
        client.return_value.request.return_value = {
            "operation": _operation("c" * 32, "rollback_previous")
        }
        self.client.force_authenticate(self.operator)

        invalid = self.csrf_post(reverse("staff-update-rollback"), {"confirmation": "yes"})
        accepted = self.csrf_post(reverse("staff-update-rollback"), {"confirmation": "ROLLBACK PREVIOUS"})

        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(accepted.status_code, status.HTTP_202_ACCEPTED)
        audit = AdminAuditLog.objects.get(action="system.update_rollback")
        self.assertEqual(audit.metadata, {"operation_id": "c" * 32})
