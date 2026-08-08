import hashlib
from dataclasses import dataclass

from django.db import transaction
from site_config.models import MediaObject, MediaStorageBackend, MediaStoragePoolSettings

from .common import MediaStorageExhausted, MediaStorageOffline, MediaStorageSetupRequired, UnsafeObjectKey, safe_object_key
from .local import DynamicLocalBackend
from .r2 import DynamicR2Backend
from .usage import account_budget, effective_account_usage, effective_storage_usage, managed_usage_bytes


@dataclass(frozen=True)
class BackendState:
    status: str
    writable: bool
    used_bytes: int | None = None
    disk_free_bytes: int | None = None
    detail: str = ""
    account_used_bytes: int | None = None


class StoragePoolService:
    @staticmethod
    def adapter_for(backend):
        if backend.backend_type == MediaStorageBackend.BackendType.CLOUDFLARE_R2:
            return DynamicR2Backend(backend)
        if backend.backend_type == MediaStorageBackend.BackendType.LOCAL:
            return DynamicLocalBackend(backend)
        raise MediaStorageOffline("不支持的媒体存储类型。")

    @classmethod
    def state_for(cls, backend, *, incoming_size_bytes=0):
        incoming_size_bytes = max(0, int(incoming_size_bytes or 0))
        if not backend.enabled:
            return BackendState("DISABLED", False, detail="后端已停用")
        if not backend.accept_new_writes:
            return BackendState("WRITE_BLOCKED", False, detail="管理员已停止新写入")
        if backend.backend_type == MediaStorageBackend.BackendType.CLOUDFLARE_R2:
            if not (
                backend.bucket_name
                and backend.endpoint_url
                and backend.public_base_url
                and backend.access_key_configured
                and backend.secret_key_configured
            ):
                return BackendState("OFFLINE", False, detail="R2 配置不完整")
            used = effective_storage_usage(backend)
            projected = used + incoming_size_bytes
            if projected > backend.write_limit_bytes:
                return BackendState("WRITE_BLOCKED", False, used_bytes=used, detail="已达到写入限制")
            account_used = effective_account_usage(backend)
            budget = account_budget(backend)
            account_limit = budget.get("write_limit_bytes")
            if account_limit is not None and account_used + incoming_size_bytes > int(account_limit):
                return BackendState("WRITE_BLOCKED", False, used_bytes=used, detail="Cloudflare Account 已达到写入限制", account_used_bytes=account_used)
            if projected >= backend.warning_bytes:
                return BackendState("WARNING", True, used_bytes=used, detail="接近写入限制", account_used_bytes=account_used)
            if budget.get("warning_bytes") is not None and account_used + incoming_size_bytes >= int(budget["warning_bytes"]):
                return BackendState("WARNING", True, used_bytes=used, detail="Cloudflare Account 接近写入限制", account_used_bytes=account_used)
            return BackendState("AVAILABLE", True, used_bytes=used, account_used_bytes=account_used)
        try:
            adapter = DynamicLocalBackend(backend)
            disk = adapter.disk_usage()
            logical = managed_usage_bytes(backend)
        except (MediaStorageOffline, UnsafeObjectKey) as error:
            return BackendState("OFFLINE", False, detail=error.detail)
        projected = logical + incoming_size_bytes
        remaining_free = int(disk.free) - incoming_size_bytes
        if projected > backend.write_limit_bytes or remaining_free <= backend.min_free_block_bytes:
            return BackendState("WRITE_BLOCKED", False, used_bytes=logical, disk_free_bytes=disk.free, detail="媒体或磁盘空间达到阻止阈值")
        if projected >= backend.warning_bytes or remaining_free <= backend.min_free_warning_bytes:
            return BackendState("WARNING", True, used_bytes=logical, disk_free_bytes=disk.free, detail="媒体或磁盘空间接近阈值")
        return BackendState("AVAILABLE", True, used_bytes=logical, disk_free_bytes=disk.free)

    @classmethod
    def resolve_effective_write_backend(cls, *, incoming_size_bytes=0):
        """Resolve the next suitable backend without changing pool preference."""
        pool = MediaStoragePoolSettings.load()
        if pool.preferred_write_backend_id:
            preferred = MediaStorageBackend.objects.filter(pk=pool.preferred_write_backend_id).first()
            if preferred and cls.state_for(preferred, incoming_size_bytes=incoming_size_bytes).writable:
                return preferred
        candidates = MediaStorageBackend.objects.filter(enabled=True, accept_new_writes=True).order_by("priority", "id")
        for candidate in candidates:
            if cls.state_for(candidate, incoming_size_bytes=incoming_size_bytes).writable:
                return candidate
        if not MediaStorageBackend.objects.exists():
            raise MediaStorageSetupRequired("尚未配置可用的媒体存储。")
        raise MediaStorageExhausted("所有媒体存储均已停止新写入。")

    @classmethod
    def select_write_backend(cls, *, incoming_size_bytes=0):
        with transaction.atomic():
            pool = MediaStoragePoolSettings.objects.select_for_update().get_or_create(pk=1)[0]
            selected = cls.resolve_effective_write_backend(incoming_size_bytes=incoming_size_bytes)
            if pool.preferred_write_backend_id != selected.pk:
                pool.preferred_write_backend = selected
                pool.save(update_fields=["preferred_write_backend", "updated_at"])
            return selected

    @classmethod
    def set_preferred_backend(cls, backend):
        state = cls.state_for(backend)
        if not state.writable:
            raise MediaStorageExhausted("目标存储当前不可写。")
        with transaction.atomic():
            pool = MediaStoragePoolSettings.objects.select_for_update().get_or_create(pk=1)[0]
            pool.preferred_write_backend = backend
            pool.save(update_fields=["preferred_write_backend", "updated_at"])
        return backend

    @classmethod
    def create_media(cls, object_key, content, *, content_type="application/octet-stream"):
        object_key = safe_object_key(object_key)
        data = bytes(content)
        backend = None
        adapter = None
        physical_write_succeeded = False
        try:
            with transaction.atomic():
                # This single row lock serializes quota decisions until the
                # matching MediaObject row is committed.
                pool = MediaStoragePoolSettings.objects.select_for_update().get_or_create(pk=1)[0]
                candidate_ids = []
                if pool.preferred_write_backend_id:
                    candidate_ids.append(pool.preferred_write_backend_id)
                candidate_ids.extend(
                    MediaStorageBackend.objects.exclude(pk__in=candidate_ids)
                    .order_by("priority", "id")
                    .values_list("pk", flat=True)
                )
                if not candidate_ids:
                    if not MediaStorageBackend.objects.exists():
                        raise MediaStorageSetupRequired("尚未配置可用的媒体存储。")
                    raise MediaStorageExhausted("所有媒体存储均已停止新写入。")
                last_error = None
                for candidate_id in candidate_ids:
                    try:
                        candidate = MediaStorageBackend.objects.select_for_update().get(pk=candidate_id)
                    except MediaStorageBackend.DoesNotExist:
                        continue
                    if not cls.state_for(candidate, incoming_size_bytes=len(data)).writable:
                        continue
                    try:
                        # Build the adapter only from the row-locked, current
                        # physical identity and credential version.
                        candidate_adapter = cls.adapter_for(candidate)
                        candidate_adapter.write(object_key, data, content_type=content_type)
                    except MediaStorageOffline as error:
                        last_error = error
                        continue
                    backend, adapter = candidate, candidate_adapter
                    physical_write_succeeded = True
                    break
                if backend is None:
                    raise MediaStorageExhausted("所有媒体存储当前均不可用。") from last_error
                media = MediaObject.objects.create(
                    storage_backend=backend,
                    object_key=object_key,
                    size_bytes=len(data),
                    content_type=str(content_type or "")[:120],
                    sha256=hashlib.sha256(data).hexdigest(),
                )
                if pool.preferred_write_backend_id != backend.pk:
                    pool.preferred_write_backend = backend
                    pool.save(update_fields=["preferred_write_backend", "updated_at"])
        except Exception:
            if physical_write_succeeded and adapter is not None:
                try:
                    adapter.delete(object_key)
                except Exception:
                    # Preserve the original transaction error; cleanup is
                    # retried by operational tooling if the backend is offline.
                    pass
            raise
        media._storage_adapter = adapter
        return media

    @classmethod
    def delete_media(cls, media):
        backend = media.storage_backend
        cls.adapter_for(backend).delete(media.object_key)
        media.delete()

    @classmethod
    def resolve_reference(cls, reference_name):
        value = str(reference_name or "")
        prefix = "media-objects/"
        if not value.startswith(prefix):
            return None
        return MediaObject.objects.select_related("storage_backend").filter(pk=value[len(prefix):]).first()

    @classmethod
    def open_reference(cls, reference_name):
        media = cls.resolve_reference(reference_name)
        if media is None:
            raise FileNotFoundError(reference_name)
        return cls.adapter_for(media.storage_backend).open(media.object_key)

    @classmethod
    def url_for_reference(cls, reference_name):
        media = cls.resolve_reference(reference_name)
        if media is None:
            raise FileNotFoundError(reference_name)
        return cls.adapter_for(media.storage_backend).url(media.object_key)

    @classmethod
    def delete_reference(cls, reference_name):
        media = cls.resolve_reference(reference_name)
        if media is not None:
            cls.delete_media(media)

    @classmethod
    def list_backends(cls):
        result = []
        for backend in MediaStorageBackend.objects.all():
            state = cls.state_for(backend)
            result.append((backend, state))
        return result
