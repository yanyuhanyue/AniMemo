from django.db import transaction
from django.db.models import Count
from django.db.models.deletion import ProtectedError
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from site_config.media_storage.common import MediaStorageError, safe_error_summary
from site_config.media_storage.pool import StoragePoolService
from site_config.media_storage.usage import (
    CLOUDFLARE_ANALYTICS_NO_DATA,
    CloudflareAnalyticsError,
    refresh_cloudflare_usage,
)
from site_config.models import CloudflareR2Account, MediaStorageBackend, MediaStoragePoolSettings

from .serializers_storage import MediaStorageBackendSerializer
from .staff_services import record_audit


class IsSuperuserOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


def storage_queryset():
    return MediaStorageBackend.objects.annotate(media_object_count=Count("media_objects")).order_by("priority", "id")


def storage_payload(request):
    pool = MediaStoragePoolSettings.load()
    items = MediaStorageBackendSerializer(storage_queryset(), many=True, context={"request": request}).data
    try:
        effective = StoragePoolService.resolve_effective_write_backend()
        effective_id = effective.pk
    except MediaStorageError:
        effective_id = None
    for item in items:
        item["is_preferred"] = item["id"] == pool.preferred_write_backend_id
        item["is_effective"] = item["id"] == effective_id
    return {
        "preferred_write_backend_id": pool.preferred_write_backend_id,
        "effective_write_backend_id": effective_id,
        "results": items,
    }


class StaffMediaStorageListView(APIView):
    permission_classes = [IsSuperuserOnly]

    def get(self, request):
        return Response(storage_payload(request))

    def post(self, request):
        serializer = MediaStorageBackendSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        backend = serializer.save()
        record_audit(
            request,
            action="storage.created",
            target=backend,
            after={"name": backend.name, "slug": backend.slug, "backend_type": backend.backend_type, "priority": backend.priority},
        )
        if getattr(serializer, "_replaced_credentials", []):
            record_audit(request, action="storage.credentials_replaced", target=backend, metadata={"fields": serializer._replaced_credentials})
        return Response(MediaStorageBackendSerializer(storage_queryset().get(pk=backend.pk)).data, status=status.HTTP_201_CREATED)


class StaffMediaStorageDetailView(APIView):
    permission_classes = [IsSuperuserOnly]

    def get_object(self, pk):
        return storage_queryset().get(pk=pk)

    def get(self, request, pk):
        return Response(MediaStorageBackendSerializer(self.get_object(pk)).data)

    def patch(self, request, pk):
        backend = self.get_object(pk)
        before = {"name": backend.name, "priority": backend.priority, "enabled": backend.enabled, "accept_new_writes": backend.accept_new_writes, "config_version": backend.config_version}
        serializer = MediaStorageBackendSerializer(backend, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        backend = serializer.save()
        after = {"name": backend.name, "priority": backend.priority, "enabled": backend.enabled, "accept_new_writes": backend.accept_new_writes, "config_version": backend.config_version}
        record_audit(request, action="storage.updated", target=backend, before=before, after=after)
        if getattr(serializer, "_replaced_credentials", []):
            record_audit(request, action="storage.credentials_replaced", target=backend, metadata={"fields": serializer._replaced_credentials})
        return Response(MediaStorageBackendSerializer(storage_queryset().get(pk=backend.pk)).data)

    def delete(self, request, pk):
        with transaction.atomic():
            backend = MediaStorageBackend.objects.select_for_update().get(pk=pk)
            if backend.media_objects.exists() or backend.media_write_reservations.filter(status="pending").exists():
                return Response({"detail": "该存储仍被媒体对象引用。", "code": "STORAGE_IN_USE"}, status=status.HTTP_409_CONFLICT)
            before = {"name": backend.name, "slug": backend.slug, "backend_type": backend.backend_type}
            try:
                backend.delete()
            except ProtectedError:
                return Response({"detail": "该存储仍被媒体对象引用。", "code": "STORAGE_IN_USE"}, status=status.HTTP_409_CONFLICT)
        record_audit(request, action="storage.deleted", target_type="MediaStorageBackend", target_id=str(pk), target_label=before["name"], before=before)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffMediaStorageActionView(APIView):
    permission_classes = [IsSuperuserOnly]

    def post(self, request, pk):
        backend = storage_queryset().get(pk=pk)
        action = str(request.data.get("action", "")).strip()
        if action == "set-active":
            try:
                StoragePoolService.set_preferred_backend(backend)
            except MediaStorageError as error:
                return Response({"detail": error.detail, "code": error.code}, status=status.HTTP_409_CONFLICT)
            record_audit(request, action="storage.write_backend_changed", target=backend, metadata={"backend_id": backend.pk})
            return Response(storage_payload(request))
        if action == "toggle-writes":
            with transaction.atomic():
                backend = MediaStorageBackend.objects.select_for_update().get(pk=pk)
                backend.accept_new_writes = bool(request.data.get("accept_new_writes", False))
                backend.config_version += 1
                backend.save(update_fields=["accept_new_writes", "config_version", "updated_at"])
            record_audit(request, action="storage.reenabled" if backend.accept_new_writes else "storage.write_blocked", target=backend)
            return Response(MediaStorageBackendSerializer(storage_queryset().get(pk=backend.pk)).data)
        if action == "clear-credentials":
            fields = set(request.data.get("fields") or [])
            changed = []
            mapping = {
                "access_key_id": "encrypted_access_key_id",
                "secret_access_key": "encrypted_secret_access_key",
            }
            with transaction.atomic():
                backend = MediaStorageBackend.objects.select_for_update().get(pk=pk)
                for name, field in mapping.items():
                    if name in fields:
                        setattr(backend, field, "")
                        changed.append(field)
                if "analytics_token" in fields and backend.cloudflare_account_ref_id:
                    account = CloudflareR2Account.objects.select_for_update().get(pk=backend.cloudflare_account_ref_id)
                    account.encrypted_analytics_token = ""
                    account.save(update_fields=["encrypted_analytics_token", "updated_at"])
                elif "analytics_token" in fields:
                    return Response({"detail": "该存储未关联 Cloudflare Account。"}, status=status.HTTP_400_BAD_REQUEST)
                if not changed and "analytics_token" not in fields:
                    return Response({"detail": "请明确指定要清除的凭证字段。"}, status=status.HTTP_400_BAD_REQUEST)
                backend.config_version += 1
                backend.save(update_fields=[*changed, "config_version", "updated_at"])
            record_audit(request, action="storage.credentials_cleared", target=backend, metadata={"fields": sorted(fields)})
            return Response(MediaStorageBackendSerializer(storage_queryset().get(pk=backend.pk)).data)
        if action == "test-connection":
            try:
                detail = StoragePoolService.adapter_for(backend).test_connection()
                ok = True
            except Exception as error:
                detail = safe_error_summary(error, "存储连接测试失败。")
                ok = False
            record_audit(request, action="storage.connection_tested", target=backend, metadata={"ok": ok})
            return Response({"ok": ok, "detail": detail}, status=status.HTTP_200_OK if ok else status.HTTP_502_BAD_GATEWAY)
        if action == "refresh-usage":
            if backend.backend_type != MediaStorageBackend.BackendType.CLOUDFLARE_R2:
                return Response({"detail": "只有 R2 存储支持 Cloudflare usage 刷新。"}, status=status.HTTP_400_BAD_REQUEST)
            try:
                refreshed, metrics = refresh_cloudflare_usage(backend.pk)
            except CloudflareAnalyticsError as error:
                current = storage_queryset().get(pk=backend.pk)
                record_audit(
                    request,
                    action="storage.usage_refreshed",
                    target=current,
                    metadata={"ok": False, "status": "FAILED", "code": error.code},
                )
                return Response(
                    {
                        "detail": f"{error.detail} 已保留上次成功快照。",
                        "code": error.code,
                        "refresh": {"status": "FAILED", "code": error.code},
                        "storage": MediaStorageBackendSerializer(current, context={"request": request}).data,
                    },
                    status=status.HTTP_424_FAILED_DEPENDENCY,
                )
            current = storage_queryset().get(pk=refreshed.pk)
            if metrics is None:
                record_audit(
                    request,
                    action="storage.usage_refreshed",
                    target=current,
                    metadata={"ok": True, "status": "NO_DATA", "code": CLOUDFLARE_ANALYTICS_NO_DATA},
                )
                return Response({
                    "detail": "Cloudflare Analytics 暂无统计数据，已保留上次成功快照。",
                    "refresh": {"status": "NO_DATA", "code": CLOUDFLARE_ANALYTICS_NO_DATA},
                    "storage": MediaStorageBackendSerializer(current, context={"request": request}).data,
                })
            record_audit(
                request,
                action="storage.usage_refreshed",
                target=current,
                metadata={"ok": True, "status": "UPDATED", "code": "CLOUDFLARE_ANALYTICS_UPDATED"},
            )
            return Response({
                "detail": "Cloudflare Analytics 容量快照已更新。",
                "refresh": {"status": "UPDATED", "code": "CLOUDFLARE_ANALYTICS_UPDATED"},
                "storage": MediaStorageBackendSerializer(current, context={"request": request}).data,
            })
        return Response({"detail": "不支持的存储操作。"}, status=status.HTTP_400_BAD_REQUEST)
