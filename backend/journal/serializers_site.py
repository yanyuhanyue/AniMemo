import ipaddress
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from rest_framework import serializers

from site_config.models import SiteSettings
from site_config.media_storage.storage import cleanup_uncommitted_media_reference, mark_media_reference_committed

from .image_security import delete_replaced_file, sanitize_uploaded_image


User = get_user_model()


HOSTNAME_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


class SiteSettingsSerializer(serializers.ModelSerializer):
    site_avatar_url = serializers.SerializerMethodField()
    turnstile = serializers.SerializerMethodField()

    class Meta:
        model = SiteSettings
        fields = [
            "site_name", "homepage_title", "site_avatar", "site_avatar_url",
            "homepage_description", "universe_description", "social_handle",
            "registration_enabled", "trusted_poster_hosts", "updated_at",
            "turnstile",
        ]
        read_only_fields = ["site_avatar_url", "updated_at"]

    def get_site_avatar_url(self, obj):
        if not obj.site_avatar:
            return ""
        request = self.context.get("request")
        url = obj.site_avatar.url
        return request.build_absolute_uri(url) if request and url.startswith("/") else url

    def get_turnstile(self, obj):
        return {
            "enabled": bool(obj.turnstile_enabled),
            "site_key": str(obj.turnstile_site_key or "").strip(),
        }

    def validate_site_avatar(self, value):
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
        previous_file = instance.site_avatar if "site_avatar" in validated_data else None
        try:
            instance = super().update(instance, validated_data)
        except Exception:
            cleanup_uncommitted_media_reference(getattr(instance.site_avatar, "name", ""))
            raise
        mark_media_reference_committed(getattr(instance.site_avatar, "name", ""))
        delete_replaced_file(previous_file, instance.site_avatar)
        return instance


class StaffSiteSettingsSerializer(SiteSettingsSerializer):
    homepage_owner_id = serializers.IntegerField(allow_null=True, required=False)
    homepage_owner_options = serializers.SerializerMethodField()
    resend_api_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        max_length=300,
    )
    clear_resend_api_key = serializers.BooleanField(write_only=True, required=False, default=False)
    resend_api_key_configured = serializers.SerializerMethodField()
    resend_api_key_source = serializers.CharField(read_only=True)
    effective_email_from = serializers.SerializerMethodField()
    email_delivery_ready = serializers.SerializerMethodField()
    turnstile_enabled = serializers.BooleanField(required=False)
    turnstile_site_key = serializers.CharField(required=False, allow_blank=True, max_length=128, trim_whitespace=True)
    turnstile_secret = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        max_length=512,
    )
    clear_turnstile_secret = serializers.BooleanField(write_only=True, required=False, default=False)
    turnstile_secret_configured = serializers.SerializerMethodField()
    turnstile_ready = serializers.SerializerMethodField()

    class Meta(SiteSettingsSerializer.Meta):
        fields = SiteSettingsSerializer.Meta.fields + [
            "homepage_owner_id",
            "homepage_owner_options",
            "email_delivery_enabled",
            "email_sender_name",
            "email_sender_address",
            "resend_api_key",
            "clear_resend_api_key",
            "resend_api_key_configured",
            "resend_api_key_source",
            "effective_email_from",
            "email_delivery_ready",
            "turnstile_enabled",
            "turnstile_site_key",
            "turnstile_secret",
            "clear_turnstile_secret",
            "turnstile_secret_configured",
            "turnstile_ready",
        ]
        read_only_fields = SiteSettingsSerializer.Meta.read_only_fields + [
            "homepage_owner_options",
            "resend_api_key_configured",
            "resend_api_key_source",
            "effective_email_from",
            "email_delivery_ready",
            "turnstile_secret_configured",
            "turnstile_ready",
        ]

    def get_homepage_owner_options(self, _obj):
        owners = User.objects.filter(is_staff=True, is_active=True).annotate(
            homepage_entry_count=Count(
                "journal_entries",
                filter=Q(journal_entries__deleted_at__isnull=True),
            ),
        ).order_by("username", "id")
        return [
            {
                "id": owner.id,
                "username": owner.username,
                "email": owner.email,
                "label": owner.get_full_name().strip() or owner.username,
                "entry_count": owner.homepage_entry_count,
            }
            for owner in owners
        ]

    def validate_homepage_owner_id(self, value):
        if value is None:
            return value
        if not User.objects.filter(pk=value, is_staff=True, is_active=True).exists():
            raise serializers.ValidationError("首页展示账号必须是已启用的管理员账号。")
        return value

    def validate_resend_api_key(self, value):
        value = value.strip()
        if value and not value.startswith("re_"):
            raise serializers.ValidationError("Resend API Key 应以 re_ 开头。")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        enabled = attrs.get("turnstile_enabled", self.instance.turnstile_enabled)
        site_key = str(attrs.get("turnstile_site_key", self.instance.turnstile_site_key) or "").strip()
        secret_input = attrs.get("turnstile_secret")
        has_new_secret = secret_input is not None and bool(str(secret_input).strip())
        clear_secret = bool(attrs.get("clear_turnstile_secret", False))
        has_secret = self.instance.turnstile_secret_configured
        if has_new_secret:
            has_secret = True
        if clear_secret:
            has_secret = False
        if enabled and not site_key:
            raise serializers.ValidationError({"turnstile_site_key": "启用 Turnstile 时必须填写 Site Key。"})
        if enabled and not has_secret:
            raise serializers.ValidationError({"turnstile_secret": "启用 Turnstile 时必须配置 Secret Key。"})
        if enabled and clear_secret and not has_new_secret:
            raise serializers.ValidationError({"clear_turnstile_secret": "Turnstile 启用时不能清除 Secret Key。"})
        if enabled and not has_new_secret and not clear_secret and self.instance.turnstile_secret_encrypted:
            try:
                if not self.instance.get_turnstile_secret():
                    raise ValueError
            except Exception as error:
                raise serializers.ValidationError({"turnstile_secret": "现有 Secret Key 无法解密，请重新填写。"}) from error
        attrs["turnstile_site_key"] = site_key
        return attrs

    def validate_trusted_poster_hosts(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("可信图片域名必须是数组。")
        hosts = []
        for raw_value in value:
            host = str(raw_value or "").strip().lower().rstrip(".")
            if not host:
                continue
            if "://" in host or "/" in host or len(host) > 253:
                raise serializers.ValidationError("请只填写域名，不要包含协议或路径。")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise serializers.ValidationError("可信图片来源不能使用 IP 地址。")
            if not HOSTNAME_PATTERN.fullmatch(host):
                raise serializers.ValidationError("可信图片来源必须是完整域名，不能包含通配符、端口或特殊字符。")
            if host not in hosts:
                hosts.append(host)
        if not hosts:
            raise serializers.ValidationError("至少保留一个可信图片域名。")
        return hosts[:30]

    def update(self, instance, validated_data):
        api_key = validated_data.pop("resend_api_key", "")
        clear_api_key = validated_data.pop("clear_resend_api_key", False)
        turnstile_secret = validated_data.pop("turnstile_secret", "")
        clear_turnstile_secret = validated_data.pop("clear_turnstile_secret", False)
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            update_fields = set()
            if clear_api_key:
                instance.resend_api_key_encrypted = ""
                update_fields.add("resend_api_key_encrypted")
            elif api_key:
                instance.set_resend_api_key(api_key)
                update_fields.add("resend_api_key_encrypted")
            if clear_turnstile_secret:
                instance.turnstile_secret_encrypted = ""
                update_fields.add("turnstile_secret_encrypted")
            elif str(turnstile_secret or "").strip():
                instance.set_turnstile_secret(turnstile_secret)
                update_fields.add("turnstile_secret_encrypted")
            if update_fields:
                update_fields.add("updated_at")
                instance.save(update_fields=list(update_fields))
        return instance

    def get_resend_api_key_configured(self, obj):
        return obj.resend_api_key_source != "none"

    def get_turnstile_secret_configured(self, obj):
        return obj.turnstile_secret_configured

    def get_turnstile_ready(self, obj):
        return obj.turnstile_ready

    def get_effective_email_from(self, obj):
        return obj.get_email_from()

    def get_email_delivery_ready(self, obj):
        return obj.email_delivery_enabled and obj.resend_api_key_source != "none"


class TestEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
