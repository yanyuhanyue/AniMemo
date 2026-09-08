import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from site_config.models import MediaObject, MediaStorageBackend, MediaStoragePoolSettings, MediaWriteReservation

from .common import MediaStorageExhausted, MediaStorageOffline, MediaStorageSetupRequired, UnsafeObjectKey, safe_object_key
from .local import DynamicLocalBackend
from .r2 import DynamicR2Backend
from .receipts import cleanup_rolled_back_upload, physical_transaction
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
    def _reserve_media(cls, object_key, data, *, content_type, sha256, excluded_backend_ids):
        now = timezone.now()
        expires_at = now + timedelta(
            seconds=max(60, int(getattr(settings, "MEDIA_WRITE_RESERVATION_TTL_SECONDS", 3600)))
        )
        with physical_transaction():
            # The existing physical reservation commits independently of any
            # outer business transaction. Storage I/O does not hold pool locks.
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
                raise MediaStorageExhausted("所有媒体存储当前均不可用。")
            for candidate_id in candidate_ids:
                if candidate_id in excluded_backend_ids:
                    continue
                try:
                    candidate = MediaStorageBackend.objects.select_for_update().get(pk=candidate_id)
                except MediaStorageBackend.DoesNotExist:
                    continue
                if not cls.state_for(candidate, incoming_size_bytes=len(data)).writable:
                    continue
                if (MediaObject.objects.filter(storage_backend=candidate, object_key=object_key).exists()
                    or MediaWriteReservation.objects.filter(storage_backend=candidate, object_key=object_key).exclude(status="abandoned").exists()):
                    raise MediaStorageExhausted("媒体对象位置已被占用。")
                reservation = MediaWriteReservation.objects.create(
                    storage_backend=candidate,
                    object_key=object_key,
                    size_bytes=len(data),
                    content_type=str(content_type or "")[:120],
                    sha256=sha256,
                    expires_at=expires_at,
                )
                if pool.preferred_write_backend_id != candidate.pk:
                    pool.preferred_write_backend = candidate
                    pool.save(update_fields=["preferred_write_backend", "updated_at"])
                return reservation, candidate
        raise MediaStorageExhausted("所有媒体存储当前均不可用。")

    @staticmethod
    def _finalize_reservation(reservation):
        with transaction.atomic():
            current = MediaWriteReservation.objects.select_for_update().get(pk=reservation.pk)
            if current.status != MediaWriteReservation.Status.PENDING:
                raise MediaStorageExhausted("媒体写入预留已不再有效。")
            from .identity import absolute_public_url, url_identity_digest

            public_url = absolute_public_url(StoragePoolService.adapter_for(current.storage_backend).url(current.object_key))
            media = MediaObject.objects.create(
                id=current.pk,
                storage_backend=current.storage_backend,
                object_key=current.object_key,
                size_bytes=current.size_bytes,
                content_type=current.content_type,
                sha256=current.sha256,
                public_url_snapshot=public_url,
                public_url_identity=url_identity_digest(public_url),
                reference_inventory_complete=True,
            )
            current.status = MediaWriteReservation.Status.FINALIZED
            current.finalized_at = timezone.now()
            current.save(update_fields=["status", "finalized_at"])
            return media

    @classmethod
    def create_media(cls, object_key, content, *, content_type="application/octet-stream"):
        object_key = safe_object_key(object_key)
        data = bytes(content)
        sha256 = hashlib.sha256(data).hexdigest()
        excluded_backend_ids = set()
        last_error = None
        while True:
            try:
                reservation, backend = cls._reserve_media(
                    object_key,
                    data,
                    content_type=content_type,
                    sha256=sha256,
                    excluded_backend_ids=excluded_backend_ids,
                )
            except MediaStorageExhausted as error:
                if last_error is not None:
                    raise MediaStorageExhausted("所有媒体存储当前均不可用。") from last_error
                raise error

            adapter = None
            try:
                adapter = cls.adapter_for(backend)
                adapter.reservation_id = reservation.pk
                adapter.write(object_key, data, content_type=content_type)
            except MediaStorageOffline as error:
                last_error = error
                excluded_backend_ids.add(backend.pk)
                cleanup_rolled_back_upload(reservation, adapter or cls.adapter_for(backend))
                continue
            except Exception:
                cleanup_rolled_back_upload(reservation, adapter or cls.adapter_for(backend))
                raise

            try:
                media = cls._finalize_reservation(reservation)
            except Exception:
                cleanup_rolled_back_upload(reservation, adapter)
                raise
            break
        media._storage_adapter = adapter
        media._write_reservation = reservation
        return media

    @classmethod
    def delete_media(cls, media):
        from journal.media_references import cleanup_media, schedule_media_cleanup

        if transaction.get_connection().in_atomic_block:
            schedule_media_cleanup(media.pk)
            return
        return cleanup_media(media.pk)

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
