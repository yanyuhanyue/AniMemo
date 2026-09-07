import copy
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from requests import Response as ProviderResponse
from rest_framework.test import APITestCase
from site_config.models import InstallationState

from journal.data_bundle import DataBundleError, export_data_bundle, import_data_bundle, preview_data_bundle
from journal.domain_services import JournalEntryService
from journal.models import ExternalMediaIdentity, JournalEntry, WatchHistoryRecord
from journal.serializers_entries import JournalEntrySerializer
from journal.watch_history import replace_history

User = get_user_model()


def encoded(bundle):
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def semantics(bundle):
    return {key: value for key, value in bundle.items() if key != "exported_at"}


class DataBundleRoundTripBoundariesTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.source = User.objects.create_user(username="portable-source")
        cls.target = User.objects.create_user(username="portable-target")

    def setUp(self):
        cache.clear()

    def populate(self, user, count):
        entries = JournalEntry.objects.bulk_create([
            JournalEntry(
                user=user,
                title=f"映画・星河 🎬 {index}",
                japanese_title=f"銀河の記憶 {index}",
                airing_period="2026-09",
                studio="記録スタジオ",
                episodes="12",
                description=f"第 {index} 部的完整描述\n第二行",
                tags=["共同标签", f"类别 {index % 7}"],
                tag_colors={"共同标签": "#ff6b6b"},
                personal_score="0.00" if index % 2 else "8.50",
                watch_status=JournalEntry.WatchStatus.values[index % 5],
                review=f"第 {index} 部的感想 ✨",
                visibility=JournalEntry.Visibility.values[index % 3],
            )
            for index in range(count)
        ])
        for index in {0, count - 1} if count else ():
            entry = entries[index]
            ExternalMediaIdentity.objects.bulk_create([
                ExternalMediaIdentity(
                    entry=entry,
                    provider=provider,
                    external_id=f"portable-{index}",
                    canonical_url=f"https://example.test/{provider}/{index}",
                    metadata={"title": entry.title, "original": {"labels": ["日本語", "中文", "🌠"]}},
                    metadata_schema_version=1,
                    is_metadata_source=provider == "bangumi",
                    metadata_fetched_at=timezone.now(),
                )
                for provider in ("bangumi", "anilist")
            ])
            replace_history(user=user, entry=entry, records=[
                {"watched_on": "2026-09-01", "brush_number": 1, "brush_label": "首刷", "notes": ["最初的感受 🌠"], "metadata": {"origin": "manual"}},
                {"watched_on": "2026-09-02", "brush_number": 2, "brush_label": "二刷", "episode_start": 1, "episode_end": 3, "notes": ["第二次的感受"], "metadata": {"custom": [1, False]}},
            ])
        return entries

    def assert_restored(self, source, target, first):
        self.assertEqual(semantics(export_data_bundle(user=target)), semantics(first))
        self.assertEqual(semantics(export_data_bundle(user=source)), semantics(first), "Import must not change the source owner")
        self.assertEqual(JournalEntry.objects.filter(user=target).count(), len(first["entries"]))
        self.assertEqual(
            ExternalMediaIdentity.objects.filter(entry__user=target).count(),
            sum(len(item["external_identities"]) for item in first["entries"]),
        )
        self.assertEqual(
            WatchHistoryRecord.objects.filter(entry__user=target).count(),
            sum(len(item["watch_history"]) for item in first["entries"]),
        )

    def test_zero_one_five_hundred_five_hundred_one_and_one_thousand_roundtrip(self):
        for count in (0, 1, 500, 501, 1000):
            with self.subTest(count=count):
                source = User.objects.create_user(username=f"source-{count}")
                target = User.objects.create_user(username=f"target-{count}")
                self.populate(source, count)
                first = export_data_bundle(user=source)
                self.assertLess(len(encoded(first)), settings.IMPORT_FILE_MAX_BYTES)
                preview = preview_data_bundle(user=target, payload=first)
                self.assertEqual((preview["total"], preview["ready"]), (count, count))
                self.assertFalse(JournalEntry.objects.filter(user=target).exists())
                result = import_data_bundle(user=target, payload=first)
                self.assertEqual(result["created"], count)
                self.assert_restored(source, target, first)

    def test_compact_http_export_with_one_thousand_entries_uploads_and_restores(self):
        self.populate(self.source, 1000)
        self.client.force_authenticate(self.source)
        exported = self.client.get("/api/export/")
        self.assertEqual(exported.status_code, 200)
        self.assertGreater(len(exported.content), settings.IMPORT_MAX_LINE_LENGTH)
        self.assertLess(len(exported.content), settings.IMPORT_FILE_MAX_BYTES)

        self.client.force_authenticate(self.target)
        upload = SimpleUploadedFile("portable.json", exported.content, content_type="application/json")
        preview = self.client.post("/api/import/?preview=true", {"file": upload}, format="multipart")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["ready"], 1000)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())
        upload = SimpleUploadedFile("portable.json", exported.content, content_type="application/json")
        restored = self.client.post("/api/import/", {"file": upload}, format="multipart")
        self.assertEqual(restored.status_code, 201, restored.data)
        self.assert_restored(self.source, self.target, exported.data)

    def test_http_json_restores_five_hundred_one_entries(self):
        self.populate(self.source, 501)
        first = export_data_bundle(user=self.source)
        self.client.force_authenticate(self.target)
        response = self.client.post("/api/import/", first, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_restored(self.source, self.target, first)

    def test_runtime_writes_all_long_tags_and_colors_then_bundle_restores_them(self):
        tags = ["星" * 101, *[f"标签 {index}" for index in range(35)]]
        colors = {tag: f"color(display-p3 0.2 0.4 0.6 / {index + 1}%)" for index, tag in enumerate(tags)}
        colors["historic-metadata"] = {"original": ["red", None, 7, True]}
        self.client.force_authenticate(self.source)
        response = self.client.post("/api/entries/", {
            "title": "长标签及颜色",
            "tags": tags,
            "tag_colors": colors,
            "visibility": "unlisted",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        entry = JournalEntry.objects.get(user=self.source)
        self.assertEqual(entry.tags, tags, "A supported write must not silently discard tags")
        self.assertEqual(entry.tag_colors, colors)
        first = export_data_bundle(user=self.source)
        import_data_bundle(user=self.target, payload=first)
        self.assert_restored(self.source, self.target, first)

    def test_existing_typed_values_are_preserved_without_import_normalization(self):
        JournalEntry.objects.create(
            user=self.source,
            title="  既有标题  ",
            japanese_title="  既存のタイトル  ",
            description="\n 原有段落与缩进 \n",
            review=" 原有短评 \n",
            tags=["旧" * 101, " 保留边界空白 ", "", "重复", "重复", *[f"历史标签 {index}" for index in range(31)]],
            tag_colors={f"历史标签 {index}": "  color(display-p3 0.1 0.2 0.3)  " for index in range(40)},
            visibility="public",
        )
        first = export_data_bundle(user=self.source)
        import_data_bundle(user=self.target, payload=first)
        self.assert_restored(self.source, self.target, first)

    def test_non_object_colors_are_rejected_by_runtime_and_bundle_before_writes(self):
        self.populate(self.source, 1)
        for colors in (["legacy", {"value": 1}], "legacy-color", True):
            with self.subTest(colors=colors):
                self.client.force_authenticate(self.target)
                response = self.client.post("/api/entries/", {"title": "无效颜色类型", "tag_colors": colors}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                payload = export_data_bundle(user=self.source)
                payload["entries"][0]["entry"]["tag_colors"] = colors
                with self.assertRaises(DataBundleError):
                    import_data_bundle(user=self.target, payload=payload)
                self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    def test_historic_color_values_readable_by_core_restore_without_conversion(self):
        colors = [[], "", False, 0, [["旧标签", "color(display-p3 1 0 0)"], ["metadata", {"original": [1, False]}]]]
        for index, value in enumerate(colors):
            with self.subTest(value=value):
                source = User.objects.create_user(username=f"legacy-color-source-{index}")
                target = User.objects.create_user(username=f"legacy-color-target-{index}")
                entry = JournalEntry.objects.create(user=source, title="既有可读颜色", tag_colors=value)
                # The former write serializer accepted JSON; the Core DTO is the
                # remaining constraint on a successfully accepted historical value.
                self.assertEqual(JournalEntryService(source).get(entry.pk)["tag_colors"], dict(value or {}))
                first = export_data_bundle(user=source)
                import_data_bundle(user=target, payload=first)
                self.assert_restored(source, target, first)
                self.assertEqual(JournalEntry.objects.get(user=target).tag_colors, value)

    def test_real_provider_unicode_snapshot_roundtrips_with_the_aggregate_utf8_budget(self):
        state = InstallationState.load()
        state.status = InstallationState.Status.INITIALIZED
        state.save(update_fields=["status"])
        subject = {
            "id": 990001,
            "name": "🌠" * 500,
            "name_cn": "🎬" * 500,
            "summary": "🌌" * 5000,
            "eps": 12,
            "date": "2026-09-01",
            "tags": [{"name": f"{index}" + "星" * 99} for index in range(8)],
        }
        calls = []

        def provider_transport(method, url, **kwargs):
            self.assertEqual(method, "get")
            self.assertTrue(kwargs["stream"])
            self.assertIn(url, {
                "https://api.bgm.tv/v0/subjects/990001",
                "https://api.bgm.tv/v0/subjects/990001/persons",
            })
            calls.append(url)
            response = ProviderResponse()
            response.status_code = 200
            response._content = encoded([] if url.endswith("/persons") else subject)
            response._content_consumed = True
            response.headers["Content-Length"] = str(len(response.content))
            return response

        self.client.force_authenticate(self.source)
        with patch("journal.bangumi.client.requests.request", side_effect=provider_transport):
            written = self.client.post("/api/v1/entries/", {
                "title": "真实规格的 Unicode 资料快照",
                "external_identity": {"provider": "bangumi", "external_id": "990001"},
            }, format="json")
        self.assertEqual(written.status_code, 201, written.data)
        self.assertEqual(len(calls), 2)
        identity = ExternalMediaIdentity.objects.get(entry__user=self.source)
        self.assertEqual(identity.metadata["summary"], subject["summary"])
        self.assertEqual(identity.metadata["title"], subject["name_cn"])
        self.assertEqual(identity.metadata["japanese_title"], subject["name"])
        self.assertEqual(identity.metadata["tags"], [item["name"] for item in subject["tags"]])
        self.assertGreater(len(json.dumps(identity.metadata, ensure_ascii=True).encode("utf-8")), 64 * 1024)

        exported = self.client.get("/api/v1/export/")
        self.assertEqual(exported.status_code, 200)
        first = exported.data
        self.assertLess(len(exported.content), settings.IMPORT_FILE_MAX_BYTES)
        self.client.force_authenticate(self.target)
        preview = self.client.post("/api/v1/import/?preview=true", first, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["ready"], 1)
        restored = self.client.post("/api/v1/import/", first, format="json")
        self.assertEqual(restored.status_code, 201, restored.data)
        self.assert_restored(self.source, self.target, first)

        empty = User.objects.create_user(username="provider-budget-target")
        expanded = copy.deepcopy(first)
        second = copy.deepcopy(first["entries"][0])
        second["external_identities"][0]["external_id"] = "990002"
        expanded["entries"].append(second)
        with override_settings(IMPORT_FILE_MAX_BYTES=len(encoded(first)) + 100):
            self.assertEqual(preview_data_bundle(user=empty, payload=first)["ready"], 1)
            with patch.object(JournalEntryService, "create_from_fields") as create:
                with self.assertRaises(DataBundleError):
                    import_data_bundle(user=empty, payload=expanded)
                create.assert_not_called()
        self.assertFalse(JournalEntry.objects.filter(user=empty).exists())

    def test_nested_type_and_domain_limits_reject_the_complete_bundle_before_writes(self):
        self.populate(self.source, 2)
        first = export_data_bundle(user=self.source)
        invalid = [
            ("entry", "title", "界" * 201),
            ("entry", "tags", ["有效", {"invalid": "tag"}]),
            ("entry", "personal_score", "10.01"),
            ("identity", "metadata", ["not-an-object"]),
            ("identity", "metadata", {"large": "x" * settings.IMPORT_FILE_MAX_BYTES}),
            ("history", "notes", ["x"] * 21),
            ("history", "notes", ["x" * 501]),
            ("history", "metadata", {"large": "x" * 4096}),
            ("history", "episode_end", 32768),
        ]
        for level, field, value in invalid:
            with self.subTest(level=level, field=field):
                payload = copy.deepcopy(first)
                item = payload["entries"][1]
                destination = {"entry": item["entry"], "identity": item["external_identities"][0], "history": item["watch_history"][0]}[level]
                destination[field] = value
                with patch.object(JournalEntryService, "create_from_fields") as create:
                    with self.assertRaises(DataBundleError):
                        import_data_bundle(user=self.target, payload=payload)
                    create.assert_not_called()
        payload = copy.deepcopy(first)
        payload["entries"][1]["watch_history"] *= 251
        with self.assertRaises(DataBundleError):
            import_data_bundle(user=self.target, payload=payload)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    def test_invalid_json_values_are_rejected_by_services_and_deep_http_payloads(self):
        self.populate(self.source, 1)
        for value in (float("nan"), float("inf")):
            payload = export_data_bundle(user=self.source)
            payload["entries"][0]["entry"]["tag_colors"] = {"invalid": value}
            with self.subTest(value=value), self.assertRaises(DataBundleError):
                import_data_bundle(user=self.target, payload=payload)
        payload["entries"][0]["entry"]["tag_colors"] = payload
        with self.assertRaises(DataBundleError):
            import_data_bundle(user=self.target, payload=payload)

        self.client.force_authenticate(self.target)
        raw = b'{"deep":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
        response = self.client.generic("POST", "/api/import/", raw, content_type="application/json")
        self.assertEqual(response.status_code, 400, response.data)
        upload = SimpleUploadedFile("deep.json", raw, content_type="application/json")
        response = self.client.post("/api/import/", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    @override_settings(IMPORT_MAX_LINE_LENGTH=30)
    def test_csv_retains_its_line_budget_while_json_uses_total_bytes(self):
        self.populate(self.source, 1)
        payload = export_data_bundle(user=self.source)
        self.client.force_authenticate(self.target)
        upload = SimpleUploadedFile("portable.json", encoded(payload), content_type="application/json")
        response = self.client.post("/api/import/?preview=true", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        upload = SimpleUploadedFile("portable.csv", b"title\n" + b"a" * 31, content_type="text/csv")
        response = self.client.post("/api/import/?preview=true", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    def test_maximum_supported_history_and_large_identity_metadata_roundtrip(self):
        entry = JournalEntry.objects.create(user=self.source, title="历史边界")
        records = [
            {"watched_on": (date(2025, 1, 1) + timedelta(days=index)).isoformat(), "brush_number": 32767, "brush_label": "重看", "episode_start": 1, "episode_end": 32767}
            for index in range(500)
        ]
        records[0]["notes"] = ["感" * 500 for _ in range(20)]
        metadata = {"value": "x" * (96 * 1024)}
        ExternalMediaIdentity.objects.create(
            entry=entry, provider="bangumi", external_id="123", canonical_url="https://bgm.tv/subject/123",
            metadata=metadata, metadata_schema_version=1, is_metadata_source=True,
        )
        replace_history(user=self.source, entry=entry, records=records)
        first = export_data_bundle(user=self.source)
        import_data_bundle(user=self.target, payload=first)
        self.assert_restored(self.source, self.target, first)

    def test_unknown_fields_at_every_portable_level_are_rejected_before_writes(self):
        self.populate(self.source, 1)
        first = export_data_bundle(user=self.source)
        for level in ("bundle", "item", "entry", "identity", "history"):
            with self.subTest(level=level):
                payload = copy.deepcopy(first)
                item = payload["entries"][0]
                destination = {
                    "bundle": payload,
                    "item": item,
                    "entry": item["entry"],
                    "identity": item["external_identities"][0],
                    "history": item["watch_history"][0],
                }[level]
                destination["owner_id"] = self.source.pk
                with patch.object(JournalEntryService, "create_from_fields") as create:
                    with self.assertRaises(DataBundleError):
                        import_data_bundle(user=self.target, payload=payload)
                    create.assert_not_called()
                self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    def test_duplicate_identities_and_conflicting_memories_are_preflight_errors(self):
        self.populate(self.source, 2)
        first = export_data_bundle(user=self.source)
        conflicts = []
        identities = copy.deepcopy(first)
        identities["entries"][1]["external_identities"][0] = copy.deepcopy(identities["entries"][0]["external_identities"][0])
        conflicts.append(identities)
        histories = copy.deepcopy(first)
        second_memory = copy.deepcopy(histories["entries"][0]["watch_history"][0])
        second_memory["notes"] = ["同一次观看的不同感受不能被覆盖"]
        histories["entries"][0]["watch_history"].append(second_memory)
        conflicts.append(histories)
        for payload in conflicts:
            with self.subTest(payload=payload["entries"][0]["entry"]["title"]):
                with patch.object(JournalEntryService, "create_from_fields") as create:
                    with self.assertRaises(DataBundleError):
                        import_data_bundle(user=self.target, payload=payload)
                    create.assert_not_called()
                self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    def test_failure_after_first_entry_rolls_back_all_entries_and_relations(self):
        self.populate(self.source, 2)
        first = export_data_bundle(user=self.source)
        calls = 0

        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertEqual(JournalEntry.objects.filter(user=self.target).count(), 2)
                self.assertTrue(ExternalMediaIdentity.objects.filter(entry__user=self.target).exists())
                self.assertTrue(WatchHistoryRecord.objects.filter(entry__user=self.target).exists())
                raise RuntimeError("synthetic mid-import failure")
            return replace_history(**kwargs)

        with patch("journal.data_bundle.services.replace_history", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "synthetic mid-import failure"):
                import_data_bundle(user=self.target, payload=first)
        self.assertEqual(calls, 2)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())
        self.assertFalse(ExternalMediaIdentity.objects.filter(entry__user=self.target).exists())
        self.assertFalse(WatchHistoryRecord.objects.filter(entry__user=self.target).exists())
        self.assertEqual(semantics(export_data_bundle(user=self.source)), semantics(first))

    def test_direct_services_enforce_the_same_total_byte_budget_as_http(self):
        self.populate(self.source, 1)
        first = export_data_bundle(user=self.source)
        size = len(encoded(first))
        with override_settings(IMPORT_FILE_MAX_BYTES=size):
            self.assertEqual(preview_data_bundle(user=self.target, payload=first)["ready"], 1)
        with override_settings(IMPORT_FILE_MAX_BYTES=size - 1):
            with self.assertRaises(DataBundleError):
                preview_data_bundle(user=self.target, payload=first)
            with self.assertRaises(DataBundleError):
                import_data_bundle(user=self.target, payload=first)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())

    def test_total_byte_budget_counts_expanded_defaults_before_any_write(self):
        payload = {
            "format": "animemo-data-bundle", "schema_version": 1, "exported_at": timezone.now().isoformat(),
            "entries": [{"entry": {"title": "默认字段", "watch_status": "planned", "visibility": "private"}}] * 10,
        }
        with override_settings(IMPORT_FILE_MAX_BYTES=len(encoded(payload))):
            with patch.object(JournalEntryService, "create_from_fields") as create:
                with self.assertRaises(DataBundleError):
                    import_data_bundle(user=self.target, payload=payload)
                create.assert_not_called()

    def test_larger_legal_export_remains_complete_while_synchronous_restore_is_unavailable(self):
        # This retains evidence of the remaining large-bundle gap; it is not a claim of closure.
        review = "感" * 800000
        serializer = JournalEntrySerializer(data={"title": "同步预算之外的合法手账", "review": review})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        JournalEntryService(self.source).create(serializer)
        first = export_data_bundle(user=self.source)
        self.assertGreater(len(encoded(first)), settings.IMPORT_FILE_MAX_BYTES)
        self.assertEqual(first["entries"][0]["entry"]["review"], review)
        self.client.force_authenticate(self.target)
        response = self.client.post("/api/import/", first, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(user=self.target).exists())
        self.assertEqual(JournalEntry.objects.get(user=self.source).review, review)
