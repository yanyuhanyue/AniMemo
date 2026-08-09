import json

from django.conf import settings
from django.db import transaction
from plugin_host.models import PluginData
from plugin_host.permissions import enabled_user_plugin
from rest_framework import serializers
from site_config.media_storage.storage import (
    cleanup_uncommitted_media_reference,
    mark_media_reference_committed,
)

from .external_media.services import (
    create_prepared_identity,
    lock_identity_owner,
    prepare_identity,
)
from .image_security import delete_replaced_file, sanitize_uploaded_image
from .models import ExternalMediaIdentity, JournalEntry
from .poster_security import PosterUrlValidationError, validate_poster_url

WATCH_HISTORY_PLUGIN_SLUG = "watch-history-importer"


def _watch_history_project(user):
    return enabled_user_plugin(WATCH_HISTORY_PLUGIN_SLUG, user)


from .watch_history import (
    WatchHistoryValidationError,
    normalize_watch_history_records,
    preserve_watch_history_metadata,
)


def _validate_poster_url(value):
    try:
        return validate_poster_url(value)
    except PosterUrlValidationError as error:
        raise serializers.ValidationError(str(error)) from error


class ExternalIdentityInputField(serializers.JSONField):
    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as error:
                raise serializers.ValidationError("外部资料身份必须是 JSON 对象。") from error
        value = super().to_internal_value(data)
        if not isinstance(value, dict):
            raise serializers.ValidationError("外部资料身份必须是 JSON 对象。")
        return value


class ExternalMediaIdentitySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExternalMediaIdentity
        fields = [
            "id", "provider", "external_id", "canonical_url", "metadata",
            "metadata_fetched_at", "provider_updated_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class JournalEntrySerializer(serializers.ModelSerializer):
    watch_status_display = serializers.CharField(source="get_watch_status_display", read_only=True)
    poster = serializers.SerializerMethodField(read_only=True)
    poster_source = serializers.SerializerMethodField(read_only=True)
    clear_custom_poster = serializers.BooleanField(write_only=True, required=False, default=False)
    share_url = serializers.SerializerMethodField(read_only=True)
    watch_history = serializers.JSONField(required=False, write_only=True)
    external_identity = ExternalIdentityInputField(required=False, write_only=True, allow_null=True)
    external_identities = ExternalMediaIdentitySerializer(many=True, read_only=True)

    class Meta:
        model = JournalEntry
        fields = [
            "id", "title", "japanese_title", "airing_period", "studio", "episodes",
            "description", "poster_url", "custom_poster_url", "poster_file", "poster", "poster_source",
            "clear_custom_poster", "baike_url", "tags",
            "tag_colors", "personal_score", "watch_status", "watch_status_display", "review",
            "visibility", "share_slug", "share_url", "watch_history", "external_identity",
            "external_identities", "created_at", "updated_at",
        ]
        read_only_fields = ["share_slug", "created_at", "updated_at"]

    def validate_tags(self, value):
        if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
            raise serializers.ValidationError("标签必须是字符串数组。")
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))[:30]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        identity_data = attrs.pop("external_identity", serializers.empty)
        self._prepared_external_identity = None
        if identity_data is serializers.empty or identity_data is None:
            return attrs
        if self.instance is not None:
            raise serializers.ValidationError({"external_identity": "请使用外部资料绑定接口修改身份。"})
        provider = identity_data.get("provider")
        external_id = identity_data.get("external_id")
        if not provider or external_id in (None, ""):
            raise serializers.ValidationError({"external_identity": "provider 和 external_id 均为必填项。"})
        self._prepared_external_identity = prepare_identity(provider, external_id)
        return attrs

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["watch_history"] = self.get_watch_history(instance)
        return representation

    def validate_watch_history(self, value):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if _watch_history_project(user) is None:
            raise serializers.ValidationError("请先安装并启用观看记录导入插件。")
        try:
            return normalize_watch_history_records(value)
        except WatchHistoryValidationError as error:
            raise serializers.ValidationError(error.detail) from error

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
            request_user = getattr(self.context.get("request"), "user", None)
            if getattr(request_user, "pk", None) == obj.user_id:
                plugin = _watch_history_project(request_user)
                if plugin is not None:
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
        plugin = _watch_history_project(entry.user)
        if plugin is None:
            raise serializers.ValidationError({"watch_history": "请先安装并启用观看记录导入插件。"})
        row = PluginData.objects.filter(
            plugin=plugin,
            namespace="watch_history",
            user=entry.user,
            key=str(entry.pk),
        ).first()
        existing = row.value if row is not None and isinstance(row.value, list) else []
        normalized = preserve_watch_history_metadata(existing, history_data)
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
                if self._prepared_external_identity is not None:
                    lock_identity_owner(instance.user)
                instance.save()
                if self._prepared_external_identity is not None:
                    create_prepared_identity(instance, self._prepared_external_identity)
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
