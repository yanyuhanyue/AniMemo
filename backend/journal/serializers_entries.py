import json

from django.conf import settings
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
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


def _validate_poster_url(value):
    try:
        return validate_poster_url(value)
    except PosterUrlValidationError as error:
        raise serializers.ValidationError(str(error)) from error


def _prefetched_watch_history(obj):
    cache = getattr(obj, "_prefetched_objects_cache", {})
    return cache.get("watch_history_records") if "watch_history_records" in cache else None


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
            "metadata_schema_version", "is_metadata_source", "metadata_fetched_at",
            "provider_updated_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ExternalMediaIdentitySummarySerializer(serializers.ModelSerializer):
    provider_title = serializers.SerializerMethodField()
    provider_score = serializers.SerializerMethodField()

    class Meta:
        model = ExternalMediaIdentity
        fields = [
            "id",
            "provider",
            "external_id",
            "canonical_url",
            "is_metadata_source",
            "metadata_fetched_at",
            "provider_title",
            "provider_score",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.STR)
    def get_provider_title(self, obj) -> str:
        return str(obj.metadata.get("title") or "") if isinstance(obj.metadata, dict) else ""

    @extend_schema_field(OpenApiTypes.NUMBER)
    def get_provider_score(self, obj):
        return obj.metadata.get("score") if isinstance(obj.metadata, dict) else None


class JournalEntrySerializer(serializers.ModelSerializer):
    watch_status_display = serializers.CharField(source="get_watch_status_display", read_only=True)
    poster = serializers.SerializerMethodField(read_only=True)
    poster_source = serializers.SerializerMethodField(read_only=True)
    clear_custom_poster = serializers.BooleanField(write_only=True, required=False, default=False)
    share_url = serializers.SerializerMethodField(read_only=True)
    watch_history_count = serializers.SerializerMethodField()
    last_watched_on = serializers.SerializerMethodField()
    first_watched_on = serializers.SerializerMethodField()
    latest_episode_start = serializers.SerializerMethodField()
    latest_episode_end = serializers.SerializerMethodField()
    external_identity = ExternalIdentityInputField(required=False, write_only=True, allow_null=True)
    external_identities = serializers.SerializerMethodField()

    class Meta:
        model = JournalEntry
        fields = [
            "id", "title", "japanese_title", "airing_period", "studio", "episodes",
            "description", "poster_url", "custom_poster_url", "poster_file", "poster", "poster_source",
            "clear_custom_poster", "baike_url", "tags",
            "tag_colors", "personal_score", "watch_status", "watch_status_display", "review",
            "visibility", "share_slug", "share_url", "watch_history_count", "last_watched_on", "first_watched_on",
            "latest_episode_start", "latest_episode_end", "external_identity",
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

    @extend_schema_field(OpenApiTypes.URI)
    def get_poster(self, obj):
        request = self.context.get("request")
        if obj.poster_file:
            url = obj.poster_file.url
            return request.build_absolute_uri(url) if request and url.startswith("/") else url
        if obj.custom_poster_url:
            return obj.custom_poster_url
        return obj.poster_url

    @extend_schema_field(OpenApiTypes.STR)
    def get_poster_source(self, obj):
        if obj.poster_file:
            return "upload"
        if obj.custom_poster_url:
            return "trusted_url"
        return "default_url" if obj.poster_url else "none"

    @extend_schema_field(OpenApiTypes.INT)
    def get_watch_history_count(self, obj):
        annotated = getattr(obj, "watch_history_count", None)
        if annotated is not None:
            return annotated
        return obj.watch_history_records.count()

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_last_watched_on(self, obj):
        if hasattr(obj, "last_watched_on"):
            return obj.last_watched_on
        records = _prefetched_watch_history(obj)
        if records is None:
            return None
        return max((record.watched_on for record in records if record.watched_on), default=None)

    @extend_schema_field(OpenApiTypes.DATE)
    def get_first_watched_on(self, obj):
        if hasattr(obj, "first_watched_on"):
            return obj.first_watched_on
        records = _prefetched_watch_history(obj)
        if records is None:
            return None
        return min((record.watched_on for record in records if record.watched_on), default=None)

    @extend_schema_field(OpenApiTypes.INT)
    def get_latest_episode_start(self, obj):
        if hasattr(obj, "latest_episode_start"):
            return obj.latest_episode_start
        records = _prefetched_watch_history(obj)
        if records is None:
            return None
        latest = max(records, key=lambda record: (record.watched_on or "", record.sequence or 0, record.id or 0), default=None)
        return latest.episode_start if latest else None

    @extend_schema_field(OpenApiTypes.INT)
    def get_latest_episode_end(self, obj):
        if hasattr(obj, "latest_episode_end"):
            return obj.latest_episode_end
        records = _prefetched_watch_history(obj)
        if records is None:
            return None
        latest = max(records, key=lambda record: (record.watched_on or "", record.sequence or 0, record.id or 0), default=None)
        return latest.episode_end if latest else None

    @extend_schema_field(ExternalMediaIdentitySummarySerializer(many=True))
    def get_external_identities(self, obj):
        request = self.context.get("request")
        view = self.context.get("view")
        serializer = (
            ExternalMediaIdentitySerializer
            if getattr(view, "action", "") == "retrieve"
            else ExternalMediaIdentitySummarySerializer
        )
        return serializer(obj.external_identities.all(), many=True, context={"request": request}).data

    def create(self, validated_data):
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
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.poster_file, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.poster_file, "name", ""))
        return instance

    def update(self, instance, validated_data):
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
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.poster_file, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.poster_file, "name", ""))
        delete_replaced_file(previous_file, instance.poster_file)
        return instance

    @extend_schema_field(OpenApiTypes.URI)
    def get_share_url(self, obj):
        request = self.context.get("request")
        if not request:
            return f"/api/v1/shared/{obj.share_slug}/"
        return request.build_absolute_uri(f"/api/v1/shared/{obj.share_slug}/")
