import ipaddress
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from plugin_host.models import PluginData, PluginProject


def _watch_history_project():
    project, _ = PluginProject.objects.get_or_create(
        plugin_id="com.anime-journal.watch-history-importer",
        defaults={
            "slug": "watch-history-importer",
            "name": "忆往昔观看记录导入器",
            "description": "观看记录导入与多次观看历史存储。",
            "installation_mode": PluginProject.InstallationMode.SYSTEM,
        },
    )
    return project
from site_config.models import SiteSettings
from site_config.media_storage.storage import cleanup_uncommitted_media_reference, mark_media_reference_committed

from .image_security import delete_replaced_file, sanitize_uploaded_image
from .models import JournalEntry


def _trusted_poster_hosts():
    values = SiteSettings.load().trusted_poster_hosts or []
    return {str(value).strip().lower().rstrip(".") for value in values if str(value).strip()}


def _validate_poster_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise serializers.ValidationError("封面必须使用受信任域名的 HTTPS 地址。")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise serializers.ValidationError("封面地址不能直接使用 IP 地址。")
    if hostname not in _trusted_poster_hosts():
        raise serializers.ValidationError("该图片域名不在管理员维护的可信白名单中。")
    return value


class JournalEntrySerializer(serializers.ModelSerializer):
    watch_status_display = serializers.CharField(source="get_watch_status_display", read_only=True)
    poster = serializers.SerializerMethodField(read_only=True)
    poster_source = serializers.SerializerMethodField(read_only=True)
    clear_custom_poster = serializers.BooleanField(write_only=True, required=False, default=False)
    share_url = serializers.SerializerMethodField(read_only=True)
    watch_history = serializers.JSONField(required=False, write_only=True)

    class Meta:
        model = JournalEntry
        fields = [
            "id", "title", "japanese_title", "airing_period", "studio", "episodes",
            "description", "poster_url", "custom_poster_url", "poster_file", "poster", "poster_source",
            "clear_custom_poster", "baike_url", "tags",
            "tag_colors", "personal_score", "watch_status", "watch_status_display", "review",
            "visibility", "share_slug", "share_url", "watch_history", "created_at", "updated_at",
        ]
        read_only_fields = ["share_slug", "created_at", "updated_at"]

    def validate_tags(self, value):
        if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
            raise serializers.ValidationError("标签必须是字符串数组。")
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))[:30]

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["watch_history"] = self.get_watch_history(instance)
        return representation

    def validate_watch_history(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("观看记录必须是数组。")
        if len(value) > 500:
            raise serializers.ValidationError("单部番剧最多保存 500 条观看记录。")

        date_field = serializers.DateField()
        normalized_by_key = {}
        for index, raw_record in enumerate(value):
            if not isinstance(raw_record, dict):
                raise serializers.ValidationError(f"第 {index + 1} 条观看记录格式无效。")

            watched_on_raw = raw_record.get("watched_on")
            if not watched_on_raw:
                raise serializers.ValidationError(f"第 {index + 1} 条观看记录缺少观看日期。")
            try:
                watched_on = date_field.run_validation(watched_on_raw)
            except serializers.ValidationError as error:
                raise serializers.ValidationError(f"第 {index + 1} 条观看记录日期无效。") from error

            brush_number = self._optional_positive_integer(raw_record.get("brush_number"), "刷次", index)
            episode_start = self._optional_positive_integer(raw_record.get("episode_start"), "起始话数", index)
            episode_end = self._optional_positive_integer(raw_record.get("episode_end"), "结束话数", index)
            if episode_start is not None and episode_end is not None and episode_end < episode_start:
                raise serializers.ValidationError(f"第 {index + 1} 条观看记录的结束话数不能小于起始话数。")

            brush_label = str(raw_record.get("brush_label") or "首刷").strip()[:20] or "首刷"
            watched_label = str(raw_record.get("watched_label") or "").strip()[:80]
            if not watched_label:
                watched_label = f"{watched_on.year}年{watched_on.month}月{watched_on.day}日"

            raw_notes = raw_record.get("notes", [])
            if isinstance(raw_notes, str):
                raw_notes = [raw_notes]
            if not isinstance(raw_notes, list) or any(not isinstance(note, str) for note in raw_notes):
                raise serializers.ValidationError(f"第 {index + 1} 条观看记录的备注必须是字符串数组。")
            notes = [note.strip()[:500] for note in raw_notes if note.strip()][:20]

            normalized = {
                "watched_on": watched_on,
                "watched_label": watched_label,
                "brush_number": brush_number,
                "brush_label": brush_label,
                "episode_start": episode_start,
                "episode_end": episode_end,
                "notes": notes,
            }
            semantic_key = (watched_on, brush_label, episode_start, episode_end)
            normalized_by_key[semantic_key] = normalized
        return list(normalized_by_key.values())

    @staticmethod
    def _optional_positive_integer(value, label, index):
        if value in (None, ""):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError) as error:
            raise serializers.ValidationError(f"第 {index + 1} 条观看记录的{label}必须是正整数。") from error
        if normalized <= 0 or normalized > 32767:
            raise serializers.ValidationError(f"第 {index + 1} 条观看记录的{label}必须是 1 到 32767。")
        return normalized

    def validate_poster_url(self, value):
        return _validate_poster_url(value)

    def validate_custom_poster_url(self, value):
        return _validate_poster_url(value)

    def validate_poster_file(self, value):
        sanitized = sanitize_uploaded_image(
            value,
            max_bytes=settings.POSTER_UPLOAD_MAX_BYTES,
            max_pixels=settings.POSTER_UPLOAD_MAX_PIXELS,
            max_width=settings.POSTER_UPLOAD_MAX_WIDTH,
            max_height=settings.POSTER_UPLOAD_MAX_HEIGHT,
            output_max_width=1600,
            output_max_height=2400,
            output_quality=88,
        )
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            total = 0
            queryset = user.journal_entries.exclude(pk=getattr(self.instance, "pk", None))
            for entry in queryset.only("poster_file"):
                if not entry.poster_file:
                    continue
                try:
                    total += entry.poster_file.size
                except (OSError, ValueError):
                    continue
            if total + sanitized.size > settings.POSTER_STORAGE_QUOTA_BYTES:
                raise serializers.ValidationError("个人封面存储已达到 500MB 配额。")
        return sanitized

    def get_poster(self, obj):
        request = self.context.get("request")
        if obj.poster_file:
            url = obj.poster_file.url
            return request.build_absolute_uri(url) if request and url.startswith("/") else url
        if obj.custom_poster_url:
            return obj.custom_poster_url
        return obj.poster_url

    def get_poster_source(self, obj):
        if obj.poster_file:
            return "upload"
        if obj.custom_poster_url:
            return "trusted_url"
        return "default_url" if obj.poster_url else "none"

    def get_watch_history(self, obj):
        cache_key = "_journal_watch_history_by_entry"
        history_by_entry = self.context.get(cache_key)
        if history_by_entry is None:
            history_by_entry = {}
            plugin = _watch_history_project()
            rows = PluginData.objects.filter(
                plugin=plugin,
                namespace="watch_history",
                user=obj.user,
                key=str(obj.pk),
            ).values_list("key", "value")
            for key, value in rows:
                history_by_entry[int(key)] = value if isinstance(value, list) else []
            self.context[cache_key] = history_by_entry
        return history_by_entry.get(obj.pk, [])

    def _sync_watch_history(self, entry, history_data):
        if history_data is serializers.empty:
            return
        plugin = _watch_history_project()
        row = PluginData.objects.filter(
            plugin=plugin,
            namespace="watch_history",
            user=entry.user,
            key=str(entry.pk),
        ).first()
        existing = row.value if row is not None and isinstance(row.value, list) else []
        existing_by_key = {
            (item.get("watched_on"), item.get("brush_label"), item.get("episode_start"), item.get("episode_end")): item
            for item in existing
            if isinstance(item, dict)
        }
        normalized = []
        for record in history_data:
            item = dict(record)
            if hasattr(item.get("watched_on"), "isoformat"):
                item["watched_on"] = item["watched_on"].isoformat()
            key = (item.get("watched_on"), item.get("brush_label"), item.get("episode_start"), item.get("episode_end"))
            normalized.append({**existing_by_key.get(key, {}), **item})
        if normalized:
            if row is None:
                PluginData.objects.create(plugin=plugin, namespace="watch_history", user=entry.user, key=str(entry.pk), value=normalized)
            else:
                row.value = normalized
                row.save(update_fields=["value", "updated_at"])
        elif row is not None:
            row.delete()
        self.context.pop("_journal_watch_history_by_entry", None)

    def create(self, validated_data):
        history_data = validated_data.pop("watch_history", serializers.empty)
        validated_data.pop("clear_custom_poster", None)
        if validated_data.get("poster_file"):
            validated_data["custom_poster_url"] = ""
        instance = JournalEntry(**validated_data)
        try:
            with transaction.atomic():
                instance.save()
                self._sync_watch_history(instance, history_data)
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.poster_file, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.poster_file, "name", ""))
        return instance

    def update(self, instance, validated_data):
        history_data = validated_data.pop("watch_history", serializers.empty)
        clear_custom_poster = validated_data.pop("clear_custom_poster", False)
        replacing_file = "poster_file" in validated_data
        replacing_with_url = bool(validated_data.get("custom_poster_url")) and not replacing_file
        previous_file = instance.poster_file if clear_custom_poster or replacing_file or replacing_with_url else None
        if clear_custom_poster:
            validated_data["poster_file"] = None
            validated_data["custom_poster_url"] = ""
        elif replacing_file and validated_data.get("poster_file"):
            validated_data["custom_poster_url"] = ""
        elif replacing_with_url:
            validated_data["poster_file"] = None
        try:
            with transaction.atomic():
                instance = super().update(instance, validated_data)
                self._sync_watch_history(instance, history_data)
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.poster_file, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.poster_file, "name", ""))
        delete_replaced_file(previous_file, instance.poster_file)
        return instance

    def get_share_url(self, obj):
        request = self.context.get("request")
        if not request:
            return f"/api/shared/{obj.share_slug}/"
        return request.build_absolute_uri(f"/api/shared/{obj.share_slug}/")
