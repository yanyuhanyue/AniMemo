import hashlib
import json
import tempfile
import time
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from integrations.authentication import sign_hmac_request
from integrations.models import (
    ExternalIdentityBinding,
    IntegrationActionReceipt,
    IntegrationConnection,
    IntegrationEvent,
    IntegrationPairingCode,
)
from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginProject, PluginVersion
from plugin_host.runtime import runtime_registry
from plugin_host.services import install_for_user, upload_plugin_version
from integrations.services import IntegrationDispatchError


def make_integration_package(slug, version="1.0.0"):
    manifest = {
        "schemaVersion": 2,
        "sdkApi": 2,
        "id": f"com.example.{slug}",
        "slug": slug,
        "name": slug,
        "version": version,
        "description": "Integration test plugin",
        "author": {"name": "Example"},
        "license": "MIT",
        "installationMode": "user",
        "runtimes": ["backend"],
        "extensions": ["backend.api", "integration.actions", "integration.events"],
        "backend": {"entry": "backend/plugin.py"},
        "integrations": {
            "actions": [{"name": "echo", "description": "Echo"}],
            "events": [{"name": "notice", "description": "Notice"}],
        },
        "permissions": [],
        "hooks": [],
        "settings": [],
        "dataPolicy": {
            "storesPersonalData": False,
            "usesExternalNetwork": False,
            "acceptsFileUploads": False,
            "retainsDataOnDisable": True,
        },
    }
    source = """class Plugin:
    def __init__(self, host):
        self.host = host
        self.calls = 0
        host.integrations.register_action('echo', self.echo)
    def echo(self, context, payload):
        self.calls += 1
        if payload.get('response_bytes'):
            return {'content': 'x' * int(payload['response_bytes'])}
        return {'user': context.user.username, 'calls': self.calls, 'version': self.host.version, 'payload': payload}
    def health_check(self):
        return True
def create_plugin(host):
    return Plugin(host)
"""
    files = {
        "manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "backend/plugin.py": source.encode(),
    }
    index_files = [
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items()
    ]
    index = json.dumps(
        {
            "packageVersion": 1,
            "pluginId": manifest["id"],
            "slug": slug,
            "version": version,
            "files": index_files,
        },
        separators=(",", ":"),
    ).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("package-index.json", index)
    return output.getvalue(), manifest


class IntegrationAPITestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "password-123")
        self.other = User.objects.create_user("bob", "bob@example.com", "password-123")
        self.connection = self.make_connection("main")
        self.other_connection = self.make_connection("backup")
        self.user_client = APIClient()
        self.user_client.force_authenticate(self.user)

    def tearDown(self):
        runtime_registry.clear()
        cache.clear()

    def make_connection(self, instance_id, secret=None):
        connection = IntegrationConnection(
            provider="generic",
            instance_id=instance_id,
            name=f"Generic {instance_id}",
            key_id=f"key-{instance_id}-{uuid4().hex[:8]}",
        )
        connection.set_secret(secret or f"secret-{instance_id}")
        connection.save()
        return connection

    def signed(self, client, method, path, payload, connection, *, nonce=None, secret=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = nonce or uuid4().hex
        headers = {
            "HTTP_X_ANIMEMO_KEY_ID": connection.key_id,
            "HTTP_X_ANIMEMO_TIMESTAMP": timestamp,
            "HTTP_X_ANIMEMO_NONCE": nonce,
            "HTTP_X_ANIMEMO_SIGNATURE": sign_hmac_request(
                secret or connection.get_secret(), timestamp, nonce, method, path, body
            ),
        }
        return client.generic(method, path, data=body, content_type="application/json", **headers)

    def create_and_consume(self, connection=None, *, platform="qq", external_user_id="100"):
        connection = connection or self.connection
        code_response = self.user_client.post(
            "/api/integrations/v1/pairing-codes/",
            {"connection_id": str(connection.pk)},
            format="json",
        )
        self.assertEqual(code_response.status_code, 201)
        code = code_response.data["code"]
        response = self.signed(
            APIClient(),
            "POST",
            "/api/integrations/v1/pair/consume/",
            {"code": code, "platform": platform, "external_user_id": external_user_id, "display_name": "Alice"},
            connection,
        )
        return code_response, response

    def test_hmac_authentication_and_replay_protection(self):
        client = APIClient()
        valid = self.signed(client, "GET", "/api/integrations/v1/events/?after=0&limit=1&wait=0", {}, self.connection)
        self.assertEqual(valid.status_code, 200)

        wrong_secret = self.signed(
            APIClient(), "GET", "/api/integrations/v1/events/?after=0&limit=1&wait=0", {}, self.connection, secret="wrong"
        )
        self.assertEqual(wrong_secret.status_code, 401)

        unknown = APIClient().generic(
            "GET", "/api/integrations/v1/events/?after=0&limit=1&wait=0", HTTP_X_ANIMEMO_KEY_ID="unknown",
            HTTP_X_ANIMEMO_TIMESTAMP=str(int(time.time())), HTTP_X_ANIMEMO_NONCE=uuid4().hex,
            HTTP_X_ANIMEMO_SIGNATURE="v1=" + "0" * 64,
        )
        self.assertEqual(unknown.status_code, 401)

        self.connection.enabled = False
        self.connection.save(update_fields=["enabled"])
        disabled = self.signed(
            APIClient(), "GET", "/api/integrations/v1/events/?after=0&limit=1&wait=0", {}, self.connection
        )
        self.assertEqual(disabled.status_code, 401)
        self.connection.enabled = True
        self.connection.save(update_fields=["enabled"])

        stale_timestamp = str(int(time.time()) - 301)
        path = "/api/integrations/v1/events/?after=0&limit=1&wait=0"
        stale = APIClient().generic(
            "GET", path, HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id,
            HTTP_X_ANIMEMO_TIMESTAMP=stale_timestamp, HTTP_X_ANIMEMO_NONCE=uuid4().hex,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(self.connection.get_secret(), stale_timestamp, "stale-nonce", "GET", path, b""),
        )
        self.assertEqual(stale.status_code, 401)

        future_timestamp = str(int(time.time()) + 301)
        future_nonce = uuid4().hex
        future = APIClient().generic(
            "GET", path, HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id,
            HTTP_X_ANIMEMO_TIMESTAMP=future_timestamp, HTTP_X_ANIMEMO_NONCE=future_nonce,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(
                self.connection.get_secret(), future_timestamp, future_nonce, "GET", path, b""
            ),
        )
        self.assertEqual(future.status_code, 401)

        nonce = uuid4().hex
        first = self.signed(APIClient(), "GET", path, {}, self.connection, nonce=nonce)
        replay = self.signed(APIClient(), "GET", path, {}, self.connection, nonce=nonce)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 401)

        body_path = "/api/integrations/v1/pair/consume/"
        body_one = json.dumps({"code": "ABCDEFGH", "platform": "qq", "external_user_id": "1"}, separators=(",", ":")).encode()
        body_two = json.dumps({"code": "ABCDEFGH", "platform": "qq", "external_user_id": "2"}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        tampered_nonce = uuid4().hex
        tampered = APIClient().generic(
            "POST", body_path, data=body_two, content_type="application/json",
            HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id, HTTP_X_ANIMEMO_TIMESTAMP=timestamp,
            HTTP_X_ANIMEMO_NONCE=tampered_nonce,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(self.connection.get_secret(), timestamp, tampered_nonce, "POST", body_path, body_one),
        )
        self.assertEqual(tampered.status_code, 401)

        mismatch_timestamp = str(int(time.time()))
        mismatch_nonce = uuid4().hex
        wrong_path = APIClient().generic(
            "GET", path, HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id,
            HTTP_X_ANIMEMO_TIMESTAMP=mismatch_timestamp, HTTP_X_ANIMEMO_NONCE=mismatch_nonce,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(
                self.connection.get_secret(), mismatch_timestamp, mismatch_nonce, "GET",
                "/api/integrations/v1/events/?after=1&limit=1&wait=0", b"",
            ),
        )
        self.assertEqual(wrong_path.status_code, 401)

        method_timestamp = str(int(time.time()))
        method_nonce = uuid4().hex
        wrong_method = APIClient().generic(
            "GET", path, HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id,
            HTTP_X_ANIMEMO_TIMESTAMP=method_timestamp, HTTP_X_ANIMEMO_NONCE=method_nonce,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(
                self.connection.get_secret(), method_timestamp, method_nonce, "POST", path, b""
            ),
        )
        self.assertEqual(wrong_method.status_code, 401)

    def test_pairing_is_one_time_connection_scoped_and_private_by_default(self):
        code_response, consumed = self.create_and_consume()
        self.assertEqual(consumed.status_code, 201)
        row = IntegrationPairingCode.objects.latest("created_at")
        self.assertNotIn(code_response.data["code"].replace("-", ""), row.code_hash)
        self.assertNotIn(code_response.data["code"].replace("-", ""), row.code_lookup)
        second = self.signed(
            APIClient(), "POST", "/api/integrations/v1/pair/consume/",
            {"code": code_response.data["code"], "platform": "qq", "external_user_id": "101"}, self.connection
        )
        self.assertEqual(second.status_code, 400)

        conflict_code = self.user_client.post(
            "/api/integrations/v1/pairing-codes/",
            {"connection_id": str(self.connection.pk)},
            format="json",
        ).data["code"]
        conflict = self.signed(
            APIClient(), "POST", "/api/integrations/v1/pair/consume/",
            {"code": conflict_code, "platform": "qq", "external_user_id": "100"}, self.connection
        )
        self.assertEqual(conflict.status_code, 409)
        conflict_reuse = self.signed(
            APIClient(), "POST", "/api/integrations/v1/pair/consume/",
            {"code": conflict_code, "platform": "qq", "external_user_id": "103"}, self.connection
        )
        self.assertEqual(conflict_reuse.status_code, 400)

        _another_code, another = self.create_and_consume(self.other_connection, external_user_id="100")
        self.assertEqual(another.status_code, 201)
        self.assertEqual(ExternalIdentityBinding.objects.filter(platform="qq", external_user_id="100").count(), 2)

        expired_code = self.user_client.post(
            "/api/integrations/v1/pairing-codes/", {"connection_id": str(self.connection.pk)}, format="json"
        ).data["code"]
        IntegrationPairingCode.objects.filter(connection=self.connection, consumed_at__isnull=True).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        expired = self.signed(
            APIClient(), "POST", "/api/integrations/v1/pair/consume/",
            {"code": expired_code, "platform": "qq", "external_user_id": "102"}, self.connection
        )
        self.assertEqual(expired.status_code, 400)

    def test_user_binding_listing_and_unbinding_are_tenant_scoped(self):
        _code, consumed = self.create_and_consume()
        binding_id = consumed.data["binding_id"]
        self.assertEqual(self.user_client.get("/api/integrations/v1/bindings/").status_code, 200)
        other_client = APIClient()
        other_client.force_authenticate(self.other)
        self.assertEqual(other_client.get("/api/integrations/v1/bindings/").data["bindings"], [])
        self.assertEqual(other_client.delete(f"/api/integrations/v1/bindings/{binding_id}/").status_code, 404)
        self.assertEqual(self.user_client.delete(f"/api/integrations/v1/bindings/{binding_id}/").status_code, 204)

    def test_connections_never_return_encrypted_secret(self):
        response = self.user_client.get("/api/integrations/v1/connections/")
        self.assertEqual(response.status_code, 200)
        rendered = json.dumps(response.data)
        self.assertNotIn("encrypted_secret", rendered)
        self.assertNotIn(self.connection.get_secret(), rendered)

    def test_anonymous_pairing_is_denied_and_admin_secret_rotation_is_one_time(self):
        anonymous = APIClient().post(
            "/api/integrations/v1/pairing-codes/",
            {"connection_id": str(self.connection.pk)},
            format="json",
        )
        self.assertEqual(anonymous.status_code, 401)

        output = StringIO()
        call_command(
            "integration_connection",
            "create",
            provider="provider",
            instance_id="managed",
            name="Managed",
            stdout=output,
        )
        created = IntegrationConnection.objects.get(provider="provider", instance_id="managed")
        first_secret = created.get_secret()
        self.assertNotEqual(created.encrypted_secret, first_secret)
        self.assertIn("secret:", output.getvalue())
        old_key_id = created.key_id

        rotated_output = StringIO()
        call_command(
            "integration_connection",
            "rotate-secret",
            str(created.pk),
            stdout=rotated_output,
        )
        created.refresh_from_db()
        self.assertNotEqual(created.key_id, old_key_id)
        self.assertNotEqual(created.get_secret(), first_secret)
        self.assertIn("secret:", rotated_output.getvalue())

    def test_events_are_private_connection_isolated_and_ack_owned(self):
        ExternalIdentityBinding.objects.create(
            connection=self.connection, user=self.user, platform="qq", external_user_id="a",
            verified_at=timezone.now(),
        )
        ExternalIdentityBinding.objects.create(
            connection=self.other_connection, user=self.user, platform="qq", external_user_id="b",
            verified_at=timezone.now(),
        )
        own = IntegrationEvent.objects.create(
            connection=self.connection, user=self.user, platform="qq", external_user_id="a",
            plugin_slug="demo", event_name="notice", payload={"ok": True},
        )
        foreign = IntegrationEvent.objects.create(
            connection=self.other_connection, user=self.user, platform="qq", external_user_id="b",
            plugin_slug="demo", event_name="notice", payload={"foreign": True},
        )
        poll = self.signed(APIClient(), "GET", "/api/integrations/v1/events/?after=0&limit=50&wait=0", {}, self.connection)
        self.assertEqual([event["event_id"] for event in poll.data["events"]], [own.pk])
        wrong_ack = self.signed(APIClient(), "POST", "/api/integrations/v1/events/ack/", {"event_ids": [foreign.pk]}, self.connection)
        self.assertEqual(wrong_ack.status_code, 200)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.acked_at)
        ack = self.signed(APIClient(), "POST", "/api/integrations/v1/events/ack/", {"event_ids": [own.pk]}, self.connection)
        self.assertEqual(ack.data["acked"], 1)
        own.refresh_from_db()
        self.assertIsNotNone(own.acked_at)

    @override_settings(
        INTEGRATION_ACKED_EVENT_RETENTION_SECONDS=100,
        INTEGRATION_UNACKED_EVENT_RETENTION_SECONDS=200,
        INTEGRATION_ACTION_RECEIPT_RETENTION_SECONDS=300,
    )
    def test_cleanup_removes_expired_events_and_completed_receipts(self):
        now = timezone.now()
        event_fields = {
            "connection": self.connection,
            "user": self.user,
            "platform": "qq",
            "external_user_id": "cleanup",
            "plugin_slug": "demo",
            "event_name": "notice",
        }
        old_acked = IntegrationEvent.objects.create(
            **event_fields,
            payload={"old": "acked"},
            acked_at=now,
        )
        recent_acked = IntegrationEvent.objects.create(
            **event_fields,
            payload={"recent": "acked"},
            acked_at=now,
        )
        old_unacked = IntegrationEvent.objects.create(
            **event_fields,
            payload={"old": "unacked"},
        )
        recent_unacked = IntegrationEvent.objects.create(
            **event_fields,
            payload={"recent": "unacked"},
        )
        IntegrationEvent.objects.filter(pk=old_acked.pk).update(
            created_at=now - timedelta(seconds=101),
            acked_at=now - timedelta(seconds=101),
        )
        IntegrationEvent.objects.filter(pk=recent_acked.pk).update(
            created_at=now - timedelta(seconds=99),
            acked_at=now - timedelta(seconds=99),
        )
        IntegrationEvent.objects.filter(pk=old_unacked.pk).update(
            created_at=now - timedelta(seconds=201),
        )
        IntegrationEvent.objects.filter(pk=recent_unacked.pk).update(
            created_at=now - timedelta(seconds=199),
        )

        old_completed = IntegrationActionReceipt.objects.create(
            connection=self.connection,
            request_id="cleanup-completed",
            action="demo.echo",
            status=IntegrationActionReceipt.Status.COMPLETED,
            response_status=200,
            completed_at=now,
        )
        old_failed = IntegrationActionReceipt.objects.create(
            connection=self.connection,
            request_id="cleanup-failed",
            action="demo.echo",
            status=IntegrationActionReceipt.Status.FAILED,
            response_status=500,
            completed_at=now,
        )
        recent_completed = IntegrationActionReceipt.objects.create(
            connection=self.connection,
            request_id="cleanup-recent",
            action="demo.echo",
            status=IntegrationActionReceipt.Status.COMPLETED,
            response_status=200,
            completed_at=now,
        )
        old_pending = IntegrationActionReceipt.objects.create(
            connection=self.connection,
            request_id="cleanup-pending",
            action="demo.echo",
        )
        IntegrationActionReceipt.objects.filter(
            pk__in=(old_completed.pk, old_failed.pk)
        ).update(
            created_at=now - timedelta(seconds=301),
            completed_at=now - timedelta(seconds=301),
        )
        IntegrationActionReceipt.objects.filter(pk=recent_completed.pk).update(
            created_at=now - timedelta(seconds=299),
            completed_at=now - timedelta(seconds=299),
        )
        IntegrationActionReceipt.objects.filter(pk=old_pending.pk).update(
            created_at=now - timedelta(seconds=301),
        )

        output = StringIO()
        call_command("cleanup_integration_events", stdout=output)

        self.assertFalse(IntegrationEvent.objects.filter(pk=old_acked.pk).exists())
        self.assertFalse(IntegrationEvent.objects.filter(pk=old_unacked.pk).exists())
        self.assertTrue(IntegrationEvent.objects.filter(pk=recent_acked.pk).exists())
        self.assertTrue(IntegrationEvent.objects.filter(pk=recent_unacked.pk).exists())
        self.assertFalse(IntegrationActionReceipt.objects.filter(pk=old_completed.pk).exists())
        self.assertFalse(IntegrationActionReceipt.objects.filter(pk=old_failed.pk).exists())
        self.assertTrue(IntegrationActionReceipt.objects.filter(pk=recent_completed.pk).exists())
        self.assertTrue(IntegrationActionReceipt.objects.filter(pk=old_pending.pk).exists())
        self.assertIn("deleted acked=1 unacked=1 receipts=2", output.getvalue())


@override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
class IntegrationActionRuntimeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.root = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(PLUGIN_ROOT=Path(self.root.name))
        self.settings_override.enable()
        self.user = User.objects.create_user("action-user", password="password-123")
        self.other = User.objects.create_user("action-other", password="password-123")
        self.admin = User.objects.create_superuser("integration-admin", password="password-123")
        self.connection = IntegrationConnection(provider="generic", instance_id="action", name="Action", key_id="action-key")
        self.connection.set_secret("action-secret")
        self.connection.save()
        self.binding = ExternalIdentityBinding.objects.create(
            connection=self.connection, user=self.user, platform="qq", external_user_id="42", verified_at=timezone.now()
        )
        payload, manifest = make_integration_package("integration-action")
        self.project = PluginProject.objects.create(
            plugin_id=manifest["id"], slug=manifest["slug"], name="Integration Action", description="test", owner=self.user
        )
        upload = type("Upload", (), {"name": "integration-action.ajplugin", "read": lambda self: payload})()
        self.version, _, _ = upload_plugin_version(self.project, upload, actor=self.user)
        self.version.review_status = PluginVersion.ReviewStatus.APPROVED
        self.version.save(update_fields=["review_status"])
        PluginPackageInstaller().publish(self.version, actor=self.admin)
        install_for_user(self.project, user=self.user)

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.root.cleanup()
        cache.clear()

    def signed_action(self, request_id, payload=None, connection=None, external_user_id="42"):
        connection = connection or self.connection
        path = "/api/integrations/v1/actions/"
        body = {
            "request_id": request_id,
            "platform": "qq",
            "external_user_id": external_user_id,
            "action": "integration-action.echo",
            "payload": payload or {},
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = uuid4().hex
        return APIClient().generic(
            "POST", path, data=raw, content_type="application/json",
            HTTP_X_ANIMEMO_KEY_ID=connection.key_id,
            HTTP_X_ANIMEMO_TIMESTAMP=timestamp,
            HTTP_X_ANIMEMO_NONCE=nonce,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(connection.get_secret(), timestamp, nonce, "POST", path, raw),
        )

    def test_action_derives_user_and_receipt_is_idempotent(self):
        path = "/api/integrations/v1/actions/"
        body = {"request_id": "req-1", "platform": "qq", "external_user_id": "42", "action": "integration-action.echo", "payload": {"user_id": self.other.pk}}
        def request(body):
            raw = json.dumps(body, separators=(",", ":")).encode()
            timestamp = str(int(time.time()))
            nonce = uuid4().hex
            return APIClient().generic("POST", path, data=raw, content_type="application/json", HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id, HTTP_X_ANIMEMO_TIMESTAMP=timestamp, HTTP_X_ANIMEMO_NONCE=nonce, HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(self.connection.get_secret(), timestamp, nonce, "POST", path, raw))
        first = request(body)
        second = request({**body, "payload": {"user_id": self.other.pk, "changed": True}})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["user"], self.user.username)
        self.assertEqual(first.data["calls"], 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["calls"], 1)
        self.assertEqual(IntegrationActionReceipt.objects.get(request_id="req-1").status, "completed")

    @override_settings(INTEGRATION_ACTION_RESPONSE_MAX_BYTES=128)
    def test_oversized_action_response_is_bounded_and_replayed_as_failure(self):
        first = self.signed_action("req-large-response", {"response_bytes": 512})
        second = self.signed_action("req-large-response", {"response_bytes": 1})

        expected = {
            "code": "action_response_too_large",
            "detail": "插件动作响应超过允许大小。",
        }
        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.data, expected)
        self.assertEqual(first["X-AniMemo-Idempotent-Replay"], "false")
        self.assertEqual(second.status_code, 502)
        self.assertEqual(second.data, expected)
        self.assertEqual(second["X-AniMemo-Idempotent-Replay"], "true")
        receipt = IntegrationActionReceipt.objects.get(request_id="req-large-response")
        self.assertEqual(receipt.status, IntegrationActionReceipt.Status.FAILED)
        self.assertEqual(receipt.response_status, 502)
        self.assertEqual(receipt.response_payload, expected)

    def test_user_installation_and_connection_boundaries_are_enforced(self):
        install_for_user(self.project, user=self.other)
        self.project.user_installations.filter(user=self.user).update(enabled=False)
        denied = self.signed_action("req-disabled")
        self.assertEqual(denied.status_code, 403, getattr(denied, "data", None))
        self.project.user_installations.filter(user=self.user).update(enabled=True)
        self.connection2 = IntegrationConnection(provider="generic", instance_id="other-action", name="Other", key_id="other-action-key")
        self.connection2.set_secret("other-action-secret")
        self.connection2.save()
        foreign = self.signed_action("req-foreign", connection=self.connection2)
        self.assertEqual(foreign.status_code, 403)

        deployment = PluginDeployment.objects.get(plugin=self.project)
        deployment.enabled = False
        deployment.healthy = False
        deployment.save(update_fields=["enabled", "healthy"])
        unavailable = self.signed_action("req-unavailable")
        self.assertEqual(unavailable.status_code, 503)

    def test_draft_runtime_cannot_execute_and_upgrade_uses_current_version(self):
        payload, _manifest = make_integration_package("integration-action", version="1.1.0")
        upload = type(
            "Upload",
            (),
            {"name": "integration-action-1.1.0.ajplugin", "read": lambda self: payload},
        )()
        draft, _, _ = upload_plugin_version(self.project, upload, actor=self.user)
        PluginDeployment.objects.filter(plugin=self.project).update(current_version=draft)
        denied = self.signed_action("req-draft")
        self.assertEqual(denied.status_code, 503)
        self.assertFalse(IntegrationActionReceipt.objects.filter(request_id="req-draft").exists())

        PluginDeployment.objects.filter(plugin=self.project).update(current_version=self.version)
        draft.review_status = PluginVersion.ReviewStatus.APPROVED
        draft.save(update_fields=["review_status"])
        PluginPackageInstaller().publish(draft, actor=self.admin)
        upgraded = self.signed_action("req-upgraded")
        self.assertEqual(upgraded.status_code, 200)
        self.assertEqual(upgraded.data["version"], "1.1.0")

    def test_plugin_event_routing_uses_user_bindings_and_private_delivery(self):
        candidate = runtime_registry.ensure_current(self.project.slug)
        result = candidate.context.integrations.emit(self.user, "notice", {"ready": True})
        self.assertEqual(result["count"], 1)
        event = IntegrationEvent.objects.get(pk=result["event_ids"][0])
        self.assertEqual(event.connection_id, self.connection.pk)
        self.assertEqual(event.external_user_id, "42")
        self.assertEqual(event.route_type, IntegrationEvent.RouteType.PRIVATE)
        self.project.user_installations.filter(user=self.user).update(enabled=False)
        with self.assertRaises(IntegrationDispatchError):
            candidate.context.integrations.emit(self.user, "notice", {"blocked": True})
