import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.exceptions import APIException

from site_config.media_storage.common import UnsafeObjectKey
from site_config.media_storage.local import approved_local_root, validate_storage_subpath
from site_config.media_storage.pool import StoragePoolService
from site_config.media_storage.usage import account_actual_usage_bytes, account_managed_usage_bytes, effective_account_usage, effective_storage_usage, managed_usage_bytes
from site_config.models import CloudflareR2Account, MediaStorageBackend


BUCKET_PATTERN = re.compile(r"^(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$")


class StoragePhysicalIdentityLocked(APIException):
    status_code = 400
    default_code = "STORAGE_PHYSICAL_IDENTITY_LOCKED"

    def __init__(self):
        super().__init__({
            "code": self.default_code,
            "detail": "Storage contains media objects and its physical location cannot be changed.",
        })


def validate_https_url(value, *, label):
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise serializers.ValidationError(f"{label}必须是无账号信息的 HTTPS URL。")
    if not settings.DEBUG and (parsed.hostname == "localhost" or parsed.hostname.startswith("127.")):
        raise serializers.ValidationError(f"生产环境的{label}不能指向本机地址。")
    return value.rstrip("/")


class MediaStorageBackendSerializer(serializers.ModelSerializer):
    cloudflare_account_id = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=64)
    access_key_id = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=300, trim_whitespace=False)
    secret_access_key = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=500, trim_whitespace=False)
    analytics_token = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=500, trim_whitespace=False)
    access_key_configured = serializers.BooleanField(read_only=True)
    secret_key_configured = serializers.BooleanField(read_only=True)
    analytics_token_configured = serializers.BooleanField(read_only=True)
    media_object_count = serializers.IntegerField(read_only=True)
    state = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    account_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=120)
    account_warning_bytes = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    account_write_limit_bytes = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    account = serializers.SerializerMethodField()

    class Meta:
        model = MediaStorageBackend
        validators = []
        fields = [
            "id", "slug", "name", "backend_type", "enabled", "accept_new_writes", "priority",
            "warning_bytes", "write_limit_bytes", "config_version", "bucket_name", "endpoint_url",
            "public_base_url", "region", "cloudflare_account_id", "usage_payload_bytes",
            "usage_metadata_bytes", "usage_object_count", "usage_refreshed_at", "local_root",
            "local_public_base_url", "min_free_warning_bytes", "min_free_block_bytes",
            "access_key_id", "secret_access_key", "analytics_token", "access_key_configured",
            "secret_key_configured", "analytics_token_configured", "media_object_count", "state",
            "usage", "account_name", "account_warning_bytes", "account_write_limit_bytes", "account", "created_at", "updated_at",
        ]
        read_only_fields = [
            "config_version", "usage_payload_bytes", "usage_metadata_bytes", "usage_object_count",
            "usage_refreshed_at", "account", "created_at", "updated_at",
        ]

    def get_state(self, obj):
        state = StoragePoolService.state_for(obj)
        return {
            "status": state.status,
            "writable": state.writable,
            "used_bytes": state.used_bytes,
            "disk_free_bytes": state.disk_free_bytes,
            "account_used_bytes": state.account_used_bytes,
            "detail": state.detail,
        }

    def get_usage(self, obj):
        managed = managed_usage_bytes(obj)
        actual = obj.snapshot_bytes if obj.backend_type == MediaStorageBackend.BackendType.CLOUDFLARE_R2 and obj.usage_refreshed_at else None
        refreshed_at = obj.usage_refreshed_at
        age_seconds = None
        if refreshed_at:
            current = datetime.now(timezone.utc)
            age_seconds = max(0, int((current - refreshed_at).total_seconds()))
        return {
            "managed_bytes": managed,
            "actual_bytes": actual,
            "untracked_bytes": max(0, int(actual or 0) - managed) if actual is not None else None,
            "effective_bytes": effective_storage_usage(obj),
            "snapshot_age_seconds": age_seconds,
        }

    def get_account(self, obj):
        account = obj.cloudflare_account_ref
        if not account:
            return None
        managed = account_managed_usage_bytes(account)
        actual = account_actual_usage_bytes(account)
        has_snapshot = any(account.storage_backends.values_list("usage_refreshed_at", flat=True))
        return {
            "id": account.pk,
            "account_id": account.account_id,
            "name": account.name,
            "warning_bytes": account.warning_bytes,
            "write_limit_bytes": account.write_limit_bytes,
            "managed_bytes": managed,
            "actual_bytes": actual if has_snapshot else None,
            "effective_bytes": effective_account_usage(account),
            "analytics_token_configured": account.analytics_token_configured,
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        account = instance.cloudflare_account_ref
        data["cloudflare_account_id"] = account.account_id if account else ""
        return data

    def validate_cloudflare_account_id(self, value):
        return str(value or "").strip().lower()

    def validate_bucket_name(self, value):
        if value and not BUCKET_PATTERN.fullmatch(value):
            raise serializers.ValidationError("Bucket 名称格式不正确。")
        return value

    def validate_endpoint_url(self, value):
        return validate_https_url(value, label="R2 Endpoint")

    def validate_public_base_url(self, value):
        return validate_https_url(value, label="R2 Public URL")

    def validate_local_public_base_url(self, value):
        return validate_https_url(value, label="Local Public URL")

    def validate_local_root(self, value):
        try:
            return validate_storage_subpath(value)
        except UnsafeObjectKey as error:
            raise serializers.ValidationError(str(error)) from error

    def validate(self, attrs):
        backend_type = attrs.get("backend_type", getattr(self.instance, "backend_type", None))
        warning = int(attrs.get("warning_bytes", getattr(self.instance, "warning_bytes", 0)) or 0)
        write_limit = int(attrs.get("write_limit_bytes", getattr(self.instance, "write_limit_bytes", 0)) or 0)
        if warning <= 0 or write_limit <= warning:
            raise serializers.ValidationError({"write_limit_bytes": "写入限制必须大于警告阈值。"})
        current_account = getattr(self.instance, "cloudflare_account_ref", None)
        account_id = str(attrs.get("cloudflare_account_id", getattr(current_account, "account_id", "")) or "").strip().lower()
        account_obj = getattr(self.instance, "cloudflare_account_ref", None)
        account_warning = attrs.get("account_warning_bytes", getattr(account_obj, "warning_bytes", None))
        account_limit = attrs.get("account_write_limit_bytes", getattr(account_obj, "write_limit_bytes", None))
        if account_warning is not None and account_limit is not None and (int(account_warning) <= 0 or int(account_limit) <= int(account_warning)):
            raise serializers.ValidationError({"account_write_limit_bytes": "Cloudflare Account 写入限制必须大于警告阈值。"})
        if backend_type == MediaStorageBackend.BackendType.CLOUDFLARE_R2:
            if not account_id:
                raise serializers.ValidationError({"cloudflare_account_id": "R2 存储必须关联 Cloudflare Account。"})
            required = ("bucket_name", "endpoint_url", "public_base_url")
            for field in required:
                if not attrs.get(field, getattr(self.instance, field, "") if self.instance else ""):
                    raise serializers.ValidationError({field: "R2 存储必须填写此项。"})
            bucket = str(attrs.get("bucket_name", getattr(self.instance, "bucket_name", "")) or "").strip().lower()
            endpoint = str(attrs.get("endpoint_url", getattr(self.instance, "endpoint_url", "")) or "").rstrip("/").lower()
            duplicates = MediaStorageBackend.objects.filter(
                backend_type=MediaStorageBackend.BackendType.CLOUDFLARE_R2,
                bucket_name=bucket,
            )
            if self.instance:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.filter(cloudflare_account_ref__account_id=account_id).exists():
                raise serializers.ValidationError({"bucket_name": "同一 Cloudflare Account 的 Bucket 已被其他存储后端使用。"})
            if endpoint and duplicates.filter(endpoint_url__iexact=endpoint).exists():
                raise serializers.ValidationError({"bucket_name": "同一 R2 Endpoint + Bucket 已被其他存储后端使用。"})
        elif backend_type == MediaStorageBackend.BackendType.LOCAL:
            free_warning = int(attrs.get("min_free_warning_bytes", getattr(self.instance, "min_free_warning_bytes", 0)) or 0)
            free_block = int(attrs.get("min_free_block_bytes", getattr(self.instance, "min_free_block_bytes", 0)) or 0)
            if free_warning <= free_block:
                raise serializers.ValidationError({"min_free_warning_bytes": "磁盘警告剩余空间必须大于阻止写入剩余空间。"})
            candidate = MediaStorageBackend(
                backend_type=backend_type,
                local_root=attrs.get("local_root", getattr(self.instance, "local_root", "") if self.instance else ""),
            )
            try:
                approved_local_root(candidate)
            except (UnsafeObjectKey, ValueError) as error:
                raise serializers.ValidationError({"local_root": str(error)}) from error
            public_url = attrs.get("local_public_base_url", getattr(self.instance, "local_public_base_url", "") if self.instance else "")
            if not public_url:
                raise serializers.ValidationError({"local_public_base_url": "Local 存储必须填写公共 URL。"})
            existing = MediaStorageBackend.objects.filter(backend_type=MediaStorageBackend.BackendType.LOCAL)
            if self.instance:
                existing = existing.exclude(pk=self.instance.pk)
            candidate_root = approved_local_root(candidate)
            for item in existing:
                if approved_local_root(item) == candidate_root:
                    raise serializers.ValidationError({"local_root": "相同的 Local 实际目录已被其他存储后端使用。"})
        self._enforce_physical_identity_lock(self.instance, attrs, backend_type, account_id)
        return attrs

    @staticmethod
    def _enforce_physical_identity_lock(instance, attrs, backend_type, account_id):
        if instance is None or not instance.media_objects.exists():
            return
        changed = backend_type != instance.backend_type
        if not changed and backend_type == MediaStorageBackend.BackendType.CLOUDFLARE_R2:
            current_account_id = instance.cloudflare_account_ref.account_id if instance.cloudflare_account_ref_id else ""
            proposed = (
                str(attrs.get("bucket_name", instance.bucket_name) or "").strip().lower(),
                str(attrs.get("endpoint_url", instance.endpoint_url) or "").strip().rstrip("/").lower(),
                str(account_id or "").strip().lower(),
            )
            current = (
                str(instance.bucket_name or "").strip().lower(),
                str(instance.endpoint_url or "").strip().rstrip("/").lower(),
                str(current_account_id or "").strip().lower(),
            )
            changed = proposed != current
        elif not changed and backend_type == MediaStorageBackend.BackendType.LOCAL:
            changed = validate_storage_subpath(attrs.get("local_root", instance.local_root)) != validate_storage_subpath(instance.local_root)
        if changed:
            raise StoragePhysicalIdentityLocked()

    @staticmethod
    def _apply_credentials(instance, values, *, account=None):
        replaced = []
        access_key = values.pop("access_key_id", "").strip()
        secret_key = values.pop("secret_access_key", "").strip()
        analytics = values.pop("analytics_token", "").strip()
        if access_key:
            instance.set_access_key_id(access_key)
            replaced.append("access_key_id")
        if secret_key:
            instance.set_secret_access_key(secret_key)
            replaced.append("secret_access_key")
        if analytics:
            if account is None:
                raise serializers.ValidationError({"analytics_token": "Analytics Token 必须保存到 Cloudflare Account。"})
            account.set_analytics_token(analytics)
            account.save(update_fields=["encrypted_analytics_token", "updated_at"])
            replaced.append("analytics_token")
        return replaced

    @staticmethod
    def _account_values(values):
        return {key: values.pop(key, None) for key in ("account_name", "account_warning_bytes", "account_write_limit_bytes")}

    @staticmethod
    def _sync_account(account_id, values, analytics_token=""):
        if not account_id:
            return None
        account = CloudflareR2Account.objects.select_for_update().filter(account_id=account_id).first()
        if account is None:
            account = CloudflareR2Account(account_id=account_id, name=values.get("account_name") or f"Cloudflare {account_id}")
        elif values.get("account_name"):
            account.name = values["account_name"]
        if values.get("account_warning_bytes") is not None:
            account.warning_bytes = values["account_warning_bytes"]
        if values.get("account_write_limit_bytes") is not None:
            account.write_limit_bytes = values["account_write_limit_bytes"]
        account.save()
        if analytics_token:
            account.set_analytics_token(analytics_token)
            account.save(update_fields=["encrypted_analytics_token", "updated_at"])
        return account

    def create(self, validated_data):
        credential_values = {key: validated_data.pop(key, "") for key in ("access_key_id", "secret_access_key", "analytics_token")}
        account_values = self._account_values(validated_data)
        account_id = validated_data.pop("cloudflare_account_id", "")
        try:
            with transaction.atomic():
                account = None
                if validated_data.get("backend_type") == MediaStorageBackend.BackendType.CLOUDFLARE_R2:
                    account = self._sync_account(account_id, account_values, credential_values["analytics_token"])
                validated_data["cloudflare_account_ref"] = account
                instance = MediaStorageBackend(**validated_data)
                self._replaced_credentials = self._apply_credentials(instance, credential_values, account=account)
                instance.save()
                return instance
        except IntegrityError as error:
            raise serializers.ValidationError({"detail": "该物理存储位置已被其他后端占用。"}) from error

    def update(self, instance, validated_data):
        credential_values = {key: validated_data.pop(key, "") for key in ("access_key_id", "secret_access_key", "analytics_token")}
        account_values = self._account_values(validated_data)
        try:
            with transaction.atomic():
                locked = MediaStorageBackend.objects.select_for_update().get(pk=instance.pk)
                target_type = validated_data.get("backend_type", locked.backend_type)
                account_id = validated_data.get(
                    "cloudflare_account_id",
                    locked.cloudflare_account_ref.account_id if locked.cloudflare_account_ref_id else "",
                )
                self._enforce_physical_identity_lock(locked, validated_data, target_type, account_id)
                validated_data.pop("cloudflare_account_id", None)
                account = None
                if target_type == MediaStorageBackend.BackendType.CLOUDFLARE_R2:
                    account = self._sync_account(account_id, account_values, credential_values["analytics_token"])
                validated_data["cloudflare_account_ref"] = account
                for field, value in validated_data.items():
                    setattr(locked, field, value)
                self._replaced_credentials = self._apply_credentials(locked, credential_values, account=account)
                locked.config_version += 1
                locked.save()
                self.instance = locked
                return locked
        except IntegrityError as error:
            raise serializers.ValidationError({"detail": "该物理存储位置已被其他后端占用。"}) from error
