"""Cloudflare usage observability and strong managed-media quota accounting."""

import math

import requests
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from site_config.models import CloudflareR2Account, MediaObject, MediaStorageBackend, MediaWriteReservation


MAX_ANALYTICS_RESPONSE_BYTES = 1024 * 1024
CLOUDFLARE_ANALYTICS_NO_DATA = "CLOUDFLARE_ANALYTICS_NO_DATA"


class CloudflareAnalyticsError(ValueError):
    code = "CLOUDFLARE_ANALYTICS_QUERY_FAILED"
    detail = "Cloudflare Analytics 查询失败。"

    def __init__(self, detail=None):
        super().__init__(detail or self.detail)
        self.detail = detail or self.detail


class CloudflareAnalyticsAuthFailed(CloudflareAnalyticsError):
    code = "CLOUDFLARE_ANALYTICS_AUTH_FAILED"
    detail = "Cloudflare Analytics 认证失败，请检查 API 令牌权限。"


class CloudflareAnalyticsTimeout(CloudflareAnalyticsError):
    code = "CLOUDFLARE_ANALYTICS_TIMEOUT"
    detail = "Cloudflare Analytics 请求超时。"


class CloudflareAnalyticsQueryFailed(CloudflareAnalyticsError):
    code = "CLOUDFLARE_ANALYTICS_QUERY_FAILED"
    detail = "Cloudflare Analytics 查询失败。"


class CloudflareAnalyticsInvalidResponse(CloudflareAnalyticsError):
    code = "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE"
    detail = "Cloudflare Analytics 返回了无法识别的响应。"


def managed_usage_bytes(backend_or_id):
    """Return bytes currently indexed by Anime Journal for one backend."""
    backend_id = getattr(backend_or_id, "pk", backend_or_id)
    media_total = MediaObject.objects.filter(storage_backend_id=backend_id).aggregate(
        total=Sum("size_bytes")
    )["total"]
    reserved_total = MediaWriteReservation.objects.filter(
        storage_backend_id=backend_id,
        status=MediaWriteReservation.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).aggregate(total=Sum("size_bytes"))["total"]
    return max(0, int(media_total or 0) + int(reserved_total or 0))


def account_backends(account_or_backend):
    """Return all configured R2 buckets belonging to one Cloudflare account."""
    account = account_or_backend if isinstance(account_or_backend, CloudflareR2Account) else getattr(account_or_backend, "cloudflare_account_ref", None)
    if account is not None and getattr(account, "pk", None):
        return MediaStorageBackend.objects.filter(
            backend_type=MediaStorageBackend.BackendType.CLOUDFLARE_R2,
            cloudflare_account_ref_id=account.pk,
        )
    return MediaStorageBackend.objects.none()


def account_managed_usage_bytes(account_or_backend):
    media_total = MediaObject.objects.filter(
        storage_backend__in=account_backends(account_or_backend),
    ).aggregate(total=Sum("size_bytes"))["total"]
    reserved_total = MediaWriteReservation.objects.filter(
        storage_backend__in=account_backends(account_or_backend),
        status=MediaWriteReservation.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).aggregate(total=Sum("size_bytes"))["total"]
    return max(0, int(media_total or 0) + int(reserved_total or 0))


def account_actual_usage_bytes(account_or_backend):
    total = sum(
        int(payload or 0) + int(metadata or 0)
        for payload, metadata, refreshed_at in account_backends(account_or_backend).values_list(
            "usage_payload_bytes", "usage_metadata_bytes", "usage_refreshed_at"
        )
        if refreshed_at
    )
    return max(0, total)


def effective_account_usage(account_or_backend):
    backends = list(account_backends(account_or_backend))
    if not backends:
        return 0
    managed_rows = MediaObject.objects.filter(storage_backend__in=backends).values("storage_backend_id").annotate(total=Sum("size_bytes"))
    managed_by_backend = {row["storage_backend_id"]: max(0, int(row["total"] or 0)) for row in managed_rows}
    reserved_rows = MediaWriteReservation.objects.filter(
        storage_backend__in=backends,
        status=MediaWriteReservation.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).values("storage_backend_id").annotate(total=Sum("size_bytes"))
    for row in reserved_rows:
        managed_by_backend[row["storage_backend_id"]] = managed_by_backend.get(row["storage_backend_id"], 0) + max(0, int(row["total"] or 0))
    return sum(
        max(
            managed_by_backend.get(backend.pk, 0),
            int(backend.snapshot_bytes or 0) if backend.usage_refreshed_at else 0,
        )
        for backend in backends
    )


def account_budget(account_or_backend):
    account = account_or_backend if isinstance(account_or_backend, CloudflareR2Account) else getattr(account_or_backend, "cloudflare_account_ref", None)
    if account is None:
        return {"warning_bytes": None, "write_limit_bytes": None}
    return {
        "warning_bytes": account.warning_bytes,
        "write_limit_bytes": account.write_limit_bytes,
    }


def effective_storage_usage(backend):
    """Use managed bytes plus the last remote snapshot as a conservative guard."""
    actual = int(backend.snapshot_bytes or 0) if backend.usage_refreshed_at else 0
    return max(managed_usage_bytes(backend), actual)


def _latest_r2_group(payload):
    """Parse the exact Cloudflare account -> adaptive groups response shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise CloudflareAnalyticsInvalidResponse()
    viewer = payload["data"].get("viewer")
    if not isinstance(viewer, dict) or not isinstance(viewer.get("accounts"), list):
        raise CloudflareAnalyticsInvalidResponse()
    accounts = viewer["accounts"]
    if not accounts:
        return None
    if not isinstance(accounts[0], dict) or not isinstance(accounts[0].get("r2StorageAdaptiveGroups"), list):
        raise CloudflareAnalyticsInvalidResponse()
    groups = accounts[0]["r2StorageAdaptiveGroups"]
    if not groups:
        return None
    if any(not isinstance(item, dict) for item in groups):
        raise CloudflareAnalyticsInvalidResponse()
    # The query orders newest first. Keep a defensive sort for fixtures or API
    # responses that omit ordering, while never summing historical points.
    def datetime_value(group):
        dimensions = group.get("dimensions") if isinstance(group, dict) else None
        return str((dimensions or {}).get("datetime") or "")

    return sorted(groups, key=datetime_value, reverse=True)[0]


def _parse_latest_metrics(payload):
    group = _latest_r2_group(payload)
    if group is None:
        return None
    maximum = group.get("max")
    if not isinstance(maximum, dict):
        raise CloudflareAnalyticsInvalidResponse()
    required = ("payloadSize", "metadataSize", "objectCount")
    if any(name not in maximum for name in required):
        raise CloudflareAnalyticsInvalidResponse()

    values = []
    for name in required:
        value = maximum[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or int(value) != value
        ):
            raise CloudflareAnalyticsInvalidResponse()
        values.append(int(value))
    return {
        "payload_bytes": values[0],
        "metadata_bytes": values[1],
        "object_count": values[2],
    }


def fetch_cloudflare_usage(backend, *, timeout=10):
    token = backend.get_analytics_token()
    account_id = backend.cloudflare_account_ref.account_id if backend.cloudflare_account_ref_id else ""
    if not token:
        raise CloudflareAnalyticsAuthFailed("Cloudflare Analytics API 令牌未配置。")
    if not account_id or not backend.bucket_name:
        raise CloudflareAnalyticsQueryFailed("Cloudflare Analytics 查询配置不完整。")
    query = """
    query R2StorageUsage($accountTag: String!, $bucketName: String!) {
      viewer {
        accounts(filter: {accountTag: $accountTag}) {
          r2StorageAdaptiveGroups(
            filter: {bucketName: $bucketName}
            orderBy: [datetime_DESC]
            limit: 1
          ) {
            dimensions { datetime }
            max { payloadSize metadataSize objectCount }
          }
        }
      }
    }
    """
    try:
        response = requests.post(
            "https://api.cloudflare.com/client/v4/graphql",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "query": query,
                "variables": {
                    "accountTag": account_id,
                    "bucketName": backend.bucket_name,
                },
            },
            timeout=timeout,
        )
    except requests.Timeout as error:
        raise CloudflareAnalyticsTimeout() from error
    except requests.RequestException as error:
        raise CloudflareAnalyticsQueryFailed() from error
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code is None:
            status_code = getattr(response, "status_code", None)
        if status_code in {401, 403}:
            raise CloudflareAnalyticsAuthFailed() from error
        raise CloudflareAnalyticsQueryFailed() from error
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if callable(getattr(headers, "get", None)) else None
    try:
        reported_size = int(content_length or 0)
    except (TypeError, ValueError):
        reported_size = 0
    if reported_size > MAX_ANALYTICS_RESPONSE_BYTES:
        raise CloudflareAnalyticsInvalidResponse()
    raw_content = getattr(response, "content", None)
    if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > MAX_ANALYTICS_RESPONSE_BYTES:
        raise CloudflareAnalyticsInvalidResponse()
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise CloudflareAnalyticsInvalidResponse() from error
    if not isinstance(payload, dict):
        raise CloudflareAnalyticsInvalidResponse()
    if payload.get("errors"):
        raise CloudflareAnalyticsQueryFailed()
    return _parse_latest_metrics(payload)


def refresh_cloudflare_usage(backend_id):
    current = MediaStorageBackend.objects.select_related("cloudflare_account_ref").get(pk=backend_id)
    metrics = fetch_cloudflare_usage(current)
    if metrics is None:
        return current, None
    with transaction.atomic():
        backend = MediaStorageBackend.objects.select_for_update().get(pk=backend_id)
        backend.usage_payload_bytes = metrics["payload_bytes"]
        backend.usage_metadata_bytes = metrics["metadata_bytes"]
        backend.usage_object_count = metrics["object_count"]
        backend.usage_refreshed_at = timezone.now()
        backend.save(update_fields=[
            "usage_payload_bytes", "usage_metadata_bytes", "usage_object_count",
            "usage_refreshed_at", "updated_at",
        ])
    return backend, metrics
