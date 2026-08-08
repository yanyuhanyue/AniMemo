from django.conf import settings
from rest_framework import serializers

from accounts.models import UserSecurityProfile
from site_config.media_storage.storage import cleanup_uncommitted_media_reference, mark_media_reference_committed

from .image_security import delete_replaced_file, sanitize_uploaded_image
from .models import Column, JournalEntry, QuickFilter, UserSettings


class UserSettingsSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    avatar_url = serializers.SerializerMethodField()
    is_public = serializers.BooleanField(source="allow_sharing", read_only=True)
    is_staff = serializers.BooleanField(source="user.is_staff", read_only=True)
    is_superuser = serializers.BooleanField(source="user.is_superuser", read_only=True)
    two_factor_enabled = serializers.SerializerMethodField()

    class Meta:
        model = UserSettings
        fields = [
            "email", "username", "nickname", "avatar", "avatar_url", "showcase_subtitle",
            "accent", "theme", "default_view", "public_status", "is_public", "allow_sharing",
            "public_slug", "public_review_reason", "public_reviewed_at", "updated_at", "is_staff",
            "is_superuser", "two_factor_enabled",
        ]
        read_only_fields = ["public_status", "is_public", "allow_sharing", "public_slug", "public_review_reason", "public_reviewed_at", "updated_at"]

    def get_avatar_url(self, obj):
        if not obj.avatar:
            return ""
        request = self.context.get("request")
        return request.build_absolute_uri(obj.avatar.url) if request and obj.avatar.url.startswith("/") else obj.avatar.url

    def get_two_factor_enabled(self, obj):
        try:
            profile = obj.user.security_profile
        except UserSecurityProfile.DoesNotExist:
            profile = None
        return bool(profile and profile.two_factor_enabled)

    def validate_avatar(self, value):
        return sanitize_uploaded_image(
            value,
            max_bytes=settings.AVATAR_UPLOAD_MAX_BYTES,
            max_pixels=settings.AVATAR_UPLOAD_MAX_PIXELS,
            max_width=settings.AVATAR_UPLOAD_MAX_WIDTH,
            max_height=settings.AVATAR_UPLOAD_MAX_HEIGHT,
            output_max_width=1024,
            output_max_height=1024,
        )

    def update(self, instance, validated_data):
        previous_file = instance.avatar if "avatar" in validated_data else None
        try:
            instance = super().update(instance, validated_data)
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.avatar, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.avatar, "name", ""))
        delete_replaced_file(previous_file, instance.avatar)
        return instance

    def create(self, validated_data):
        instance = UserSettings(**validated_data)
        try:
            instance.save()
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.avatar, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.avatar, "name", ""))
        return instance


class QuickFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuickFilter
        fields = ["id", "name", "tags", "title_keywords", "match_mode", "color", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class ColumnSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    entries = serializers.PrimaryKeyRelatedField(many=True, queryset=JournalEntry.objects.all(), required=False)

    class Meta:
        model = Column
        fields = [
            "id", "title", "slug", "summary", "cover", "body", "status", "status_display",
            "featured", "entries", "author_name", "moderation_reason", "moderated_at",
            "created_at", "updated_at", "published_at",
        ]
        read_only_fields = ["slug", "status", "featured", "moderation_reason", "moderated_at", "created_at", "updated_at", "published_at"]

    def get_author_name(self, obj):
        settings = getattr(obj.author, "journal_settings", None)
        return settings.nickname if settings and settings.nickname else obj.author.get_username()

    def validate_entries(self, value):
        request = self.context.get("request")
        if request and any(entry.user_id != request.user.id for entry in value):
            raise serializers.ValidationError("只能关联自己的手账记录。")
        return value

    def validate_cover(self, value):
        return sanitize_uploaded_image(
            value,
            max_bytes=settings.COLUMN_COVER_UPLOAD_MAX_BYTES,
            max_pixels=settings.COLUMN_COVER_UPLOAD_MAX_PIXELS,
            max_width=settings.COLUMN_COVER_UPLOAD_MAX_WIDTH,
            max_height=settings.COLUMN_COVER_UPLOAD_MAX_HEIGHT,
            output_max_width=2400,
            output_max_height=2400,
        )

    def update(self, instance, validated_data):
        previous_file = instance.cover if "cover" in validated_data else None
        try:
            instance = super().update(instance, validated_data)
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.cover, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.cover, "name", ""))
        delete_replaced_file(previous_file, instance.cover)
        return instance

    def create(self, validated_data):
        entries = validated_data.pop("entries", [])
        instance = Column(**validated_data)
        try:
            instance.save()
            if entries:
                instance.entries.set(entries)
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.cover, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.cover, "name", ""))
        return instance
