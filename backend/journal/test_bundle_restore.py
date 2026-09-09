"""Portable restore HTTP, filesystem and real-transaction regression tests."""
import hashlib
import io
import json
import tempfile
import time
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from journal.data_bundle import export_data_bundle
from journal.data_bundle import restore_storage as storage
from journal.data_bundle.restore import cleanup_restores, commit_restore
from journal.models import (
    BundleRestoreAdmission,
    BundleRestoreSession,
    ExternalMediaIdentity,
    JournalEntry,
)
from journal.mutation_ports import bind_mutation_ports, restore_mutation_ports
from journal.watch_history import add_history

User = get_user_model()


def encoded(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class BundleRestoreTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.temporary = tempfile.TemporaryDirectory(prefix="animemo-bundle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.overrides = override_settings(BUNDLE_RESTORE_ROOT=Path(self.temporary.name) / "staging")
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        BundleRestoreAdmission.objects.get_or_create(pk=1)
        self.owner = User.objects.create_user(username="restore-target")
        self.source = User.objects.create_user(username="restore-source")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def bundle(self, count=1, text="完整的中文🌌"):
        for number in range(count):
            entry = JournalEntry.objects.create(user=self.source, title=f"标题 {number}", review=text,
                                                 tags=[" 保留空白 ", "重复", "重复", ""],
                                                 tag_colors={"组合": {"original": [True, 0, None]}}, visibility="private")
            ExternalMediaIdentity.objects.create(entry=entry, provider="bangumi", external_id=str(number),
                                                 canonical_url=f"https://bgm.tv/subject/{number}",
                                                 metadata={"summary": "星" * 5000}, metadata_schema_version=1)
            add_history(user=self.source, entry=entry, record={"watched_on": "2026-09-08", "notes": ["记忆🌌"]})
        return export_data_bundle(user=self.source)

    def create(self, data, *, digest=None, key=None):
        response = self.client.post("/api/v1/bundle-restores/", {
            "expected_bytes": len(data), "sha256": digest or hashlib.sha256(data).hexdigest(),
            "schema_version": 1, "idempotency_key": str(key or uuid.uuid4()),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("Authorization", response["Vary"])
        return response.data

    def upload(self, data, state):
        for offset in range(0, len(data), settings.BUNDLE_RESTORE_CHUNK_BYTES):
            response = self.chunk(state, offset, data[offset:offset + settings.BUNDLE_RESTORE_CHUNK_BYTES])
            self.assertEqual(response.status_code, 200, response.data)
            state = response.data
        return state

    def chunk(self, state, offset, data):
        return self.client.generic("PUT", f'/api/v1/bundle-restores/{state["id"]}/chunks/?offset={offset}&generation={state["generation"]}',
                                   data, content_type="application/octet-stream")

    def operation(self, state, operation):
        return self.client.post(f'/api/v1/bundle-restores/{state["id"]}/{operation}/',
                                {"generation": state["generation"]}, format="json")

    def ready(self, data):
        state = self.upload(data, self.create(data))
        response = self.operation(state, "validate")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["state"], "ready")
        return response.data

    def test_large_legal_bundle_roundtrip_preserves_all_fields_and_receipt(self):
        payload = self.bundle(text="感" * 800000)
        data = encoded(payload)
        self.assertGreater(len(data), settings.IMPORT_FILE_MAX_BYTES)
        old = self.client.generic("POST", "/api/v1/import/", data, content_type="application/json")
        self.assertEqual(old.status_code, 400)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
        state = self.ready(data)
        result = self.operation(state, "commit")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["receipt"]["created"], 1)
        self.assertEqual(export_data_bundle(user=self.owner)["entries"], payload["entries"])
        # Simulate a lost commit response: retrieve and repeat the same operation.
        queried = self.client.get(f'/api/bundle-restores/{state["id"]}/')
        repeated = self.operation(state, "commit")
        self.assertEqual(queried.data["receipt"], result.data["receipt"])
        self.assertEqual(repeated.data["receipt"], result.data["receipt"])
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 1)

    def test_chunk_duplicates_conflicts_missing_and_hash_failure(self):
        data = encoded(self.bundle(text="界" * 800000))
        state = self.create(data)
        first = data[:settings.BUNDLE_RESTORE_CHUNK_BYTES]
        self.assertEqual(self.chunk(state, 0, first).status_code, 200)
        self.assertEqual(self.chunk(state, 0, first).status_code, 200)
        self.assertEqual(self.chunk(state, 0, b"x" + first[1:]).status_code, 409)
        self.assertEqual(self.operation(state, "validate").status_code, 409)
        self.assertEqual(self.chunk(state, 2 * settings.BUNDLE_RESTORE_CHUNK_BYTES, data[2 * settings.BUNDLE_RESTORE_CHUNK_BYTES:]).status_code, 409)
        self.operation(state, "cancel")
        state = self.upload(data, self.create(data, digest="0" * 64))
        # A caller-provided wrong digest is invalid input, not disk exhaustion.
        self.assertEqual(self.operation(state, "validate").status_code, 400)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    def test_owner_is_checked_on_every_route_and_alias(self):
        data = encoded(self.bundle())
        state = self.create(data)
        self.client.force_authenticate(self.source)
        self.assertEqual(self.client.get(f'/api/bundle-restores/{state["id"]}/').status_code, 404)
        self.assertEqual(self.chunk(state, 0, data).status_code, 404)
        for operation in ("validate", "commit", "cancel"):
            self.assertEqual(self.operation(state, operation).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f'/api/v1/bundle-restores/{state["id"]}/').status_code, 401)

    def test_idempotency_binding_and_one_unfinished_session(self):
        data = encoded(self.bundle())
        key = uuid.uuid4()
        first = self.create(data, key=key)
        self.assertEqual(self.create(data, key=key)["id"], first["id"])
        for changes in ({"expected_bytes": len(data) + 1}, {"idempotency_key": str(uuid.uuid4())}):
            body = {"expected_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "schema_version": 1, "idempotency_key": str(key), **changes}
            self.assertEqual(self.client.post("/api/bundle-restores/", body, format="json").status_code, 409)

    def test_domain_failure_precedes_business_writes_and_errors_are_bounded(self):
        payload = self.bundle(count=2)
        payload["entries"][1]["entry"]["review"] = "x"
        payload["entries"][1]["entry"]["private" * 500] = "do not reflect"
        data = encoded(payload)
        state = self.upload(data, self.create(data))
        response = self.operation(state, "validate")
        self.assertEqual(response.status_code, 400)
        self.assertLessEqual(len(response.content), 16384)
        self.assertEqual(set(response.data), {"code", "detail", "correlation_id"})
        self.assertNotIn(b"do not reflect", response.content)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    def test_mid_commit_failure_rolls_back_rows_receipt_and_events(self):
        data = encoded(self.bundle(count=2))
        state = self.ready(data)
        events = []
        ports = bind_mutation_ports(policy_runner=lambda _name, value, _context: value,
                                   event_publisher=lambda name, context: events.append((name, context)))
        self.addCleanup(restore_mutation_ports, ports)
        from journal.data_bundle.restore import replace_history as original
        calls = 0
        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("synthetic failure")
            return original(**kwargs)
        with patch("journal.data_bundle.restore.replace_history", side_effect=fail_second):
            response = self.operation(state, "commit")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
        session = BundleRestoreSession.objects.get(pk=state["id"])
        self.assertEqual(session.receipt, {})
        self.assertEqual(session.state, "failed")
        self.assertEqual(events, [])

    def test_nested_database_invalid_strings_fail_full_preflight(self):
        original = self.bundle()
        for location in ("metadata_value", "metadata_key", "tag_colors", "tags", "history_notes"):
            with self.subTest(location=location):
                payload = deepcopy(original)
                item = payload["entries"][0]
                if location == "metadata_value":
                    item["external_identities"][0]["metadata"] = {"nested": [{"x": "\x00"}]}
                elif location == "metadata_key":
                    item["external_identities"][0]["metadata"] = {"nested": [{"\x00": "x"}]}
                elif location == "tag_colors":
                    item["entry"]["tag_colors"] = {"nested": ["\x00"]}
                elif location == "tags":
                    item["entry"]["tags"] = ["\x00"]
                else:
                    item["watch_history"][0]["notes"] = ["\x00"]
                data = encoded(payload)
                state = self.upload(data, self.create(data))
                response = self.operation(state, "validate")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], "invalid_data_bundle")
                session = BundleRestoreSession.objects.get(pk=state["id"])
                self.assertEqual(session.state, "failed")
                self.assertIsNone(session.operation_expires_at)
                self.assertEqual(session.receipt, {})
                self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
                cleanup_restores()

    def test_database_commit_error_rolls_back_and_releases_active_slot(self):
        state = self.ready(encoded(self.bundle(count=2)))
        from journal.data_bundle.restore import replace_history as original
        calls = 0
        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DatabaseError("synthetic database failure: do not reflect")
            return original(**kwargs)
        with patch("journal.data_bundle.restore.replace_history", side_effect=fail_second):
            response = self.operation(state, "commit")
        self.assertEqual(response.status_code, 503, response.data)
        self.assertNotIn(b"do not reflect", response.content)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
        session = BundleRestoreSession.objects.get(pk=state["id"])
        self.assertEqual(session.state, "failed")
        self.assertIsNone(session.operation_expires_at)
        self.assertEqual(session.receipt, {})

    def test_outer_rollback_keeps_ready_session_bytes_and_no_receipt(self):
        state = self.ready(encoded(self.bundle()))
        session = BundleRestoreSession.objects.get(pk=state["id"])
        before = storage.physical_size(session)
        with self.assertRaisesRegex(RuntimeError, "outer rollback"), transaction.atomic():
            result = commit_restore(user=self.owner, session_id=state["id"], generation=state["generation"])
            self.assertEqual(result["state"], "completed")
            raise RuntimeError("outer rollback")
        session.refresh_from_db()
        self.assertEqual(session.state, "ready")
        self.assertEqual(session.receipt, {})
        self.assertEqual(storage.physical_size(session), before)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    def test_post_preview_create_causes_atomic_conflict(self):
        state = self.ready(encoded(self.bundle()))
        created = self.client.post("/api/v1/entries/", {"title": "预览后的正常创建"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        response = self.operation(state, "commit")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 1)

    def test_cleanup_failure_retains_reservation_and_receipt(self):
        state = self.ready(encoded(self.bundle()))
        complete = self.operation(state, "commit").data
        session = BundleRestoreSession.objects.get(pk=state["id"])
        reserved = session.reserved_bytes
        with patch.object(Path, "unlink", side_effect=PermissionError("synthetic cleanup failure")):
            self.assertEqual(cleanup_restores()["deferred"], 1)
        session.refresh_from_db()
        self.assertEqual(session.reserved_bytes, reserved)
        self.assertEqual(session.receipt, complete["receipt"])
        self.assertEqual(cleanup_restores()["cleaned"], 1)
        session.refresh_from_db()
        self.assertEqual(session.reserved_bytes, 0)
        self.assertEqual(session.physical_bytes, 0)
        self.assertEqual(self.operation(state, "cancel").data["receipt"], complete["receipt"])
        self.assertEqual(JournalEntry.objects.filter(user=self.owner).count(), 1)

    def test_expired_operation_recovers_by_bounded_command_and_old_generation_fails(self):
        data = encoded(self.bundle())
        state = self.upload(data, self.create(data))
        BundleRestoreSession.objects.filter(pk=state["id"]).update(
            state="validating", operation_expires_at=timezone.now() - timedelta(seconds=1))
        result = cleanup_restores()
        self.assertEqual(result["expired"], 1)
        session = BundleRestoreSession.objects.get(pk=state["id"])
        self.assertEqual(session.state, "expired")
        self.assertEqual(self.operation(state, "commit").status_code, 409)

    def test_zero_entries_can_complete_with_a_zero_receipt(self):
        state = self.ready(encoded(self.bundle(count=0)))
        response = self.operation(state, "commit")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["receipt"]["created"], 0)

    def test_normalized_budget_rejects_default_expansion_before_any_write(self):
        data = encoded({"format": "animemo-data-bundle", "schema_version": 1, "exported_at": timezone.now().isoformat(),
                        "entries": [{"entry": {"title": "默认值", "watch_status": "planned", "visibility": "private"}}] * 20})
        with override_settings(BUNDLE_RESTORE_NORMALIZED_BYTES=len(data)):
            state = self.upload(data, self.create(data))
            response = self.operation(state, "validate")
        self.assertEqual(response.status_code, 413, response.data)
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    def test_retained_orphan_receipt_does_not_starve_later_cleanup(self):
        first = self.ready(encoded(self.bundle()))
        self.operation(first, "commit")
        self.assertEqual(cleanup_restores()["cleaned"], 1)
        BundleRestoreSession.objects.filter(pk=first["id"]).update(owner=None)
        later = self.create(encoded(export_data_bundle(user=self.source)))
        self.operation(later, "cancel")
        result = cleanup_restores(limit=1)
        self.assertEqual(result["cleaned"], 1, result)
        self.assertEqual(BundleRestoreSession.objects.get(pk=later["id"]).reserved_bytes, 0)
        self.assertEqual(BundleRestoreSession.objects.get(pk=first["id"]).state, "completed")

    @override_settings(BUNDLE_RESTORE_OPERATION_SECONDS=1)
    def test_validation_finishing_after_lease_does_not_publish_ready(self):
        from journal.data_bundle.restore import _validate_to_file
        data = encoded(self.bundle())
        state = self.upload(data, self.create(data))
        def finish_late(session):
            _validate_to_file(session)
            time.sleep(1.1)
        with patch("journal.data_bundle.restore._validate_to_file", side_effect=finish_late):
            response = self.operation(state, "validate")
        self.assertEqual(response.status_code, 408, response.data)
        self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).state, "failed")
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    @override_settings(BUNDLE_RESTORE_SINGLE_BYTES=200)
    def test_legal_item_over_operation_budget_reports_capacity(self):
        data = encoded(self.bundle())
        state = self.upload(data, self.create(data))
        response = self.operation(state, "validate")
        self.assertEqual(response.status_code, 413, response.data)
        self.assertEqual(response.data["code"], "bundle_restore_capacity")
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    @override_settings(BUNDLE_RESTORE_INDEX_BYTES=1)
    def test_below_minimum_identity_index_budget_does_not_publish_ready(self):
        data = encoded(self.bundle())
        state = self.upload(data, self.create(data))
        response = self.operation(state, "validate")
        self.assertEqual(response.status_code, 413, response.data)
        self.assertEqual(BundleRestoreSession.objects.get(pk=state["id"]).state, "failed")
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())

    def test_local_file_adapter_uses_same_preview_commit_and_receipt(self):
        payload = self.bundle(text="感" * 800000)
        path = Path(self.temporary.name) / "portable.json"
        data = encoded(payload)
        path.write_bytes(data)
        key = str(uuid.uuid4())
        output = io.StringIO()
        arguments = ["--owner-id", str(self.owner.pk), "--file", str(path), "--idempotency-key", key]
        call_command("restore_data_bundle", *arguments, stdout=output)
        ready = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(ready["state"], "ready")
        self.assertFalse(JournalEntry.objects.filter(user=self.owner).exists())
        output = io.StringIO()
        call_command("restore_data_bundle", *arguments, "--commit", stdout=output)
        complete = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(complete["id"], ready["id"])
        self.assertEqual(complete["receipt"]["created"], 1)
        repeated = io.StringIO()
        call_command("restore_data_bundle", *arguments, "--commit", stdout=repeated)
        self.assertEqual(json.loads(repeated.getvalue().splitlines()[-1])["receipt"], complete["receipt"])
        self.assertEqual(export_data_bundle(user=self.owner)["entries"], payload["entries"])
        self.assertEqual(path.read_bytes(), data)
