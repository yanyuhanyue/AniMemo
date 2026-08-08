import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest import skipUnless
from uuid import uuid4

from django.core.cache import cache
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from integrations.authentication import sign_hmac_request
from integrations.models import ExternalIdentityBinding, IntegrationConnection
from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginProject, PluginVersion
from plugin_host.runtime import runtime_registry
from plugin_host.services import install_for_user, upload_plugin_version

from .test_protocol import make_integration_package


@skipUnless(connection.vendor == "postgresql", "Integration receipt concurrency proof requires PostgreSQL")
@override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
class IntegrationReceiptPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.root = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(PLUGIN_ROOT=Path(self.root.name))
        self.settings_override.enable()
        self.user = User.objects.create_user("receipt-user", password="password-123")
        self.admin = User.objects.create_superuser("receipt-admin", password="password-123")
        self.connection_row = IntegrationConnection(
            provider="generic", instance_id="receipt", name="Receipt", key_id="receipt-key"
        )
        self.connection_row.set_secret("receipt-secret")
        self.connection_row.save()
        ExternalIdentityBinding.objects.create(
            connection=self.connection_row,
            user=self.user,
            platform="qq",
            external_user_id="42",
            verified_at=timezone.now(),
        )
        payload, manifest = make_integration_package("receipt-race")
        project = PluginProject.objects.create(
            plugin_id=manifest["id"], slug=manifest["slug"], name="Receipt Race", description="test", owner=self.user
        )
        upload = type("Upload", (), {"name": "receipt-race.ajplugin", "read": lambda self: payload})()
        version, _, _ = upload_plugin_version(project, upload, actor=self.user)
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        PluginPackageInstaller().publish(version, actor=self.admin)
        install_for_user(project, user=self.user)
        candidate = runtime_registry.ensure_current(project.slug)
        original = candidate.context.integrations.resolve_action("echo")
        self.handler_started = Event()
        self.release_handler = Event()

        def blocked(context, action_payload):
            self.handler_started.set()
            self.release_handler.wait(timeout=10)
            return original(context, action_payload)

        candidate.context.integrations._actions["echo"] = blocked
        self.slug = project.slug

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.root.cleanup()
        cache.clear()

    def _request(self):
        path = "/api/integrations/v1/actions/"
        body = {
            "request_id": "same-request",
            "platform": "qq",
            "external_user_id": "42",
            "action": f"{self.slug}.echo",
            "payload": {"source": "concurrent"},
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = uuid4().hex
        try:
            return APIClient().generic(
                "POST",
                path,
                data=raw,
                content_type="application/json",
                HTTP_X_ANIMEMO_KEY_ID=self.connection_row.key_id,
                HTTP_X_ANIMEMO_TIMESTAMP=timestamp,
                HTTP_X_ANIMEMO_NONCE=nonce,
                HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(
                    self.connection_row.get_secret(), timestamp, nonce, "POST", path, raw
                ),
            )
        finally:
            connections.close_all()

    def test_duplicate_request_id_executes_handler_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._request)]
            self.assertTrue(self.handler_started.wait(timeout=10))
            futures.append(pool.submit(self._request))
            time.sleep(0.2)
            self.release_handler.set()
            responses = [future.result(timeout=20) for future in futures]
        self.assertEqual({response.status_code for response in responses}, {200})
        self.assertEqual({response.data["calls"] for response in responses}, {1})
