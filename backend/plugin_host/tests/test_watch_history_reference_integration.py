import json
import tempfile
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from integrations.authentication import sign_hmac_request
from integrations.models import ExternalIdentityBinding, IntegrationConnection, IntegrationEvent
from journal.models import JournalEntry, WatchHistoryRecord
from plugin_host.models import PluginData, PluginProject
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

    def test_history_add_rejects_invalid_shared_watch_history_fields(self):
        invalid_payloads = (
            ({"watched_on": "banana"}, "invalid_watched_on"),
            ({"watched_on": "2026-08-09", "episode_start": 0}, "invalid_episode_start"),
            ({"watched_on": "2026-08-09", "episode_start": 2, "episode_end": 1}, "invalid_episode_range"),
            ({"watched_on": "2026-08-09", "episode_end": 32768}, "invalid_episode_end"),
            ({"watched_on": "2026-08-09", "notes": {"text": "invalid"}}, "invalid_notes"),
        )
        for fields, expected_code in invalid_payloads:
            with self.subTest(fields=fields):
                response = self.signed_action("history-add", {"entry_id": self.entry.pk, **fields})
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], expected_code)

        self.assertFalse(WatchHistoryRecord.objects.filter(entry=self.entry).exists())
        self.assertFalse(PluginData.objects.filter(plugin=self.project, namespace="watch_history").exists())

    def test_web_and_integration_use_identical_normalization(self):
        raw_record = {
            "watched_on": "2026-08-09",
            "watched_label": "",
            "brush_number": "2",
            "brush_label": "  二刷  ",
            "episode_start": "7",
            "episode_end": "8",
            "notes": "  补充记录  ",
        }
        integration = self.signed_action(
            "history-add", {"entry_id": self.entry.pk, **raw_record}
        )
        self.assertEqual(integration.status_code, 200, integration.data)

        web_entry = JournalEntry.objects.create(user=self.user, title="Web 端规范化")
        web_client = APIClient()
        web_client.force_authenticate(self.user)
        web = web_client.post(
            f"/api/entries/{web_entry.pk}/watch-history/",
            raw_record,
            format="json",
        )
        self.assertEqual(web.status_code, 201, web.data)
        comparable_fields = {
            "watched_on", "watched_label", "brush_number", "brush_label",
            "episode_start", "episode_end", "notes", "metadata",
        }
        self.assertEqual(
            {key: integration.data["record"][key] for key in comparable_fields},
            {key: web.data["record"][key] for key in comparable_fields},
        )

    def test_duplicate_semantic_history_record_is_idempotent(self):
        payload = {
            "entry_id": self.entry.pk,
            "watched_on": "2026-08-09",
            "brush_label": "首刷",
            "episode_start": 1,
            "episode_end": 12,
        }
        first = self.signed_action("history-add", payload)
        second = self.signed_action("history-add", payload)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertTrue(first.data["created"])
        self.assertEqual(second.status_code, 200, second.data)
        self.assertFalse(second.data["created"])
        self.assertEqual(second.data["total"], 1)

    def test_integration_import_preview_reserves_gateway_envelope_overhead(self):
        accepted = self.signed_action("import-preview", {"text": "x" * (120 * 1024)})
        self.assertEqual(accepted.status_code, 200, accepted.data)

        rejected = self.signed_action("import-preview", {"text": "x" * (120 * 1024 + 1)})
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(rejected.data["code"], "invalid_text")

    def test_import_commit_uses_shared_watch_history_validation(self):
        batch_id = uuid4().hex
        batch_row = PluginData.objects.create(
            plugin=self.project,
            namespace="batches",
            user=self.user,
            key=batch_id,
            value={
                "id": batch_id,
                "target_user_id": self.user.pk,
                "payload": {
                    "groups": [{
                        "source_title": self.entry.title,
                        "resolution": {
                            "status": "matched",
                            "bangumi_id": 123,
                            "title": self.entry.title,
                            "japanese_title": "",
                        },
                        "records": [{
                            "watch_date": "banana",
                            "watch_date_label": "",
                            "brush": 2,
                            "brush_label": "二刷",
                            "episode_range": {"start": 7, "end": 8},
                            "notes": [],
                        }],
                    }],
                },
            },
        )
        invalid = self.signed_action("import-commit", {"batch_id": batch_id})
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertEqual(invalid.data["code"], "invalid_watched_on")
        self.assertFalse(WatchHistoryRecord.objects.filter(entry=self.entry).exists())
        self.assertFalse(PluginData.objects.filter(plugin=self.project, namespace="watch_history").exists())

        batch = batch_row.value
        batch["payload"]["groups"][0]["records"][0] = {
            "watch_date": "2026-08-09",
            "watch_date_label": "",
            "brush": "2",
            "brush_label": "  二刷  ",
            "episode_range": {"start": "7", "end": "8"},
            "notes": "  导入备注  ",
        }
        batch_row.value = batch
        batch_row.save(update_fields=["value", "updated_at"])
        committed = self.signed_action("import-commit", {"batch_id": batch_id})
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertEqual(committed.data["imported_records"], 1)
        history = list(WatchHistoryRecord.objects.filter(entry=self.entry).values(
            "watched_on", "watched_label", "brush_number", "brush_label",
            "episode_start", "episode_end", "notes", "metadata",
        ))
        self.assertEqual(history, [{
            "watched_on": date(2026, 8, 9),
            "watched_label": "2026年8月9日",
            "brush_number": 2,
            "brush_label": "二刷",
            "episode_start": 7,
            "episode_end": 8,
            "notes": ["导入备注"],
            "metadata": {},
        }])
        self.assertFalse(PluginData.objects.filter(plugin=self.project, namespace="watch_history").exists())

    def test_web_import_commit_uses_shared_watch_history_normalization(self):
        batch_id = uuid4().hex
        PluginData.objects.create(
            plugin=self.project,
            namespace="batches",
            user=self.user,
            key=batch_id,
            value={
                "id": batch_id,
                "status": "ready",
                "summary": {},
                "target_user_id": self.user.pk,
                "payload": {
                    "groups": [{
                        "source_title": self.entry.title,
                        "resolution": {
                            "status": "matched",
                            "bangumi_id": 456,
                            "title": self.entry.title,
                            "japanese_title": "",
                            "tags": [],
                        },
                        "records": [{
                            "watch_date": "2026-08-10",
                            "watch_date_label": "",
                            "brush": "3",
                            "brush_label": "  三刷  ",
                            "episode_range": {"start": "1", "end": "12"},
                            "notes": "  Web 导入备注  ",
                        }],
                    }],
                },
            },
        )
        web_client = APIClient()
        web_client.force_authenticate(self.user)
        response = web_client.post(
            f"/api/plugins/watch-history-importer/batches/{batch_id}/commit/",
            {"excluded_group_indices": []},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        history = WatchHistoryRecord.objects.get(entry=self.entry)
        self.assertEqual({
            "watched_on": history.watched_on,
            "watched_label": history.watched_label,
            "brush_number": history.brush_number,
            "brush_label": history.brush_label,
            "episode_start": history.episode_start,
            "episode_end": history.episode_end,
            "notes": history.notes,
            "metadata": history.metadata,
        }, {
            "watched_on": date(2026, 8, 10),
            "watched_label": "2026年8月10日",
            "brush_number": 3,
            "brush_label": "三刷",
            "episode_start": 1,
            "episode_end": 12,
            "notes": ["Web 导入备注"],
            "metadata": {},
        })
        self.assertFalse(PluginData.objects.filter(plugin=self.project, namespace="watch_history").exists())

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
