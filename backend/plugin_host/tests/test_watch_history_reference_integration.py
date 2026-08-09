import json
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from integrations.authentication import sign_hmac_request
from integrations.models import ExternalIdentityBinding, IntegrationConnection, IntegrationEvent
from journal.models import JournalEntry
from plugin_host.models import PluginProject
from plugin_host.runtime import runtime_registry
from plugin_host.services import install_for_user


@override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
class WatchHistoryReferenceIntegrationTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name))
        self.settings.enable()
        self.user = User.objects.create_user("reference-user", password="password-123")
        self.other = User.objects.create_user("reference-other", password="password-123")
        self.admin = User.objects.create_superuser("reference-admin", password="password-123")
        self.connection = IntegrationConnection(
            provider="astrbot",
            instance_id="reference-test",
            name="AstrBot Reference",
            key_id=f"reference-key-{uuid4().hex[:8]}",
        )
        self.connection.set_secret("reference-secret")
        self.connection.save()
        self.binding = ExternalIdentityBinding.objects.create(
            connection=self.connection,
            user=self.user,
            platform="telegram",
            external_user_id="42",
            verified_at=timezone.now(),
        )
        call_command("sync_official_plugins", verbosity=0)
        self.project = PluginProject.objects.get(slug="watch-history-importer")
        install_for_user(self.project, user=self.user)
        self.entry = JournalEntry.objects.create(user=self.user, title="葬送的芙莉莲")
        self.foreign_entry = JournalEntry.objects.create(user=self.other, title="别人的番剧")

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    def signed_action(self, action, payload, request_id=None):
        request_id = request_id or uuid4()
        body = {
            "request_id": str(request_id),
            "platform": "telegram",
            "external_user_id": "42",
            "action": f"watch-history-importer.{action}",
            "payload": payload,
        }
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        path = "/api/integrations/v1/actions/"
        timestamp = str(int(time.time()))
        nonce = uuid4().hex
        return APIClient().generic(
            "POST",
            path,
            data=raw,
            content_type="application/json",
            HTTP_X_ANIMEMO_KEY_ID=self.connection.key_id,
            HTTP_X_ANIMEMO_TIMESTAMP=timestamp,
            HTTP_X_ANIMEMO_NONCE=nonce,
            HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(
                self.connection.get_secret(), timestamp, nonce, "POST", path, raw
            ),
        )

    def test_history_actions_are_bound_to_current_user_and_emit_sanitized_event(self):
        added = self.signed_action(
            "history-add",
            {"entry_id": self.entry.pk, "watched_on": "2026-08-09", "episode_start": 7, "episode_end": 7},
        )
        self.assertEqual(added.status_code, 200, added.data)
        self.assertTrue(added.data["created"])
        event = IntegrationEvent.objects.get(plugin_slug="watch-history-importer", event_name="history-updated")
        self.assertEqual(event.user_id, self.user.pk)
        self.assertNotIn("external_user_id", event.payload)
        self.assertNotIn("connection_id", event.payload)
        fetched = self.signed_action("history-get", {"entry_id": self.entry.pk})
        self.assertEqual(fetched.status_code, 200, fetched.data)
        self.assertEqual(len(fetched.data["records"]), 1)

    def test_entries_search_does_not_cross_tenant(self):
        response = self.signed_action("entries-search", {"query": "芙莉莲"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item["entry_id"] for item in response.data["entries"]], [self.entry.pk])
        forbidden = self.signed_action("history-get", {"entry_id": self.foreign_entry.pk})
        self.assertEqual(forbidden.status_code, 404)

    def test_disabled_user_installation_is_rejected(self):
        self.project.user_installations.filter(user=self.user).update(enabled=False)
        response = self.signed_action("history-get", {"entry_id": self.entry.pk})
        self.assertEqual(response.status_code, 403)
