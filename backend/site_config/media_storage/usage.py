"""Cloudflare usage observability and strong managed-media quota accounting."""

import requests
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from site_config.models import CloudflareR2Account, MediaObject, MediaStorageBackend


MAX_ANALYTICS_RESPONSE_BYTES = 1024 * 1024


def managed_usage_bytes(backend_or_id):
    """Return bytes currently indexed by Anime Journal for one backend."""
    backend_id = getattr(backend_or_id, "pk", backend_or_id)
    total = MediaObject.objects.filter(storage_backend_id=backend_id).aggregate(
        total=Sum("size_bytes")
    )["total"]
    return max(0, int(total or 0))


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
    total = MediaObject.objects.filter(
        storage_backend__in=account_backends(account_or_backend),
    ).aggregate(total=Sum("size_bytes"))["total"]
    return max(0, int(total or 0))


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


def effective_r2_usage(backend):
    """Compatibility name for callers that still describe the R2 guard."""
    return effective_storage_usage(backend)


def _latest_r2_group(payload):
    """Parse the exact Cloudflare account -> adaptive groups response shape."""
    data = payload.get("data") if isinstance(payload, dict) else None
    viewer = data.get("viewer") if isinstance(data, dict) else None
    accounts = viewer.get("accounts") if isinstance(viewer, dict) else None
    if not isinstance(accounts, list) or not accounts:
        return None
    groups = accounts[0].get("r2StorageAdaptiveGroups") if isinstance(accounts[0], dict) else None
    if not isinstance(groups, list) or not groups:
        return None
    # The query orders newest first. Keep a defensive sort for fixtures or API
    # responses that omit ordering, while never summing historical points.
    def datetime_value(group):
        dimensions = group.get("dimensions") if isinstance(group, dict) else None
        return str((dimensions or {}).get("datetime") or "")

    return sorted((item for item in groups if isinstance(item, dict)), key=datetime_value, reverse=True)[0] if groups else None


def _parse_latest_metrics(payload):
    group = _latest_r2_group(payload)
    maximum = group.get("max") if isinstance(group, dict) else None
    if not isinstance(maximum, dict):
        return None
    required = ("payloadSize", "metadataSize", "objectCount")
    if any(name not in maximum for name in required):
        return None
    try:
        return {
            "payload_bytes": max(0, int(maximum.get("payloadSize") or 0)),
            "metadata_bytes": max(0, int(maximum.get("metadataSize") or 0)),
            "object_count": max(0, int(maximum.get("objectCount") or 0)),
        }
    except (TypeError, ValueError):
        return None


def fetch_cloudflare_usage(backend, *, timeout=10):
    token = backend.get_analytics_token()
    account_id = backend.cloudflare_account_ref.account_id if backend.cloudflare_account_ref_id else ""
    if not account_id or not token or not backend.bucket_name:
        raise ValueError("Cloudflare Analytics 配置不完整。")
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
    response.raise_for_status()
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if callable(getattr(headers, "get", None)) else None
    try:
        reported_size = int(content_length or 0)
    except (TypeError, ValueError):
        reported_size = 0
    if reported_size > MAX_ANALYTICS_RESPONSE_BYTES:
        raise ValueError("Cloudflare Analytics 响应过大。")
    raw_content = getattr(response, "content", None)
    if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > MAX_ANALYTICS_RESPONSE_BYTES:
        raise ValueError("Cloudflare Analytics 响应过大。")
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise ValueError("Cloudflare Analytics 响应不是有效 JSON。") from error
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError("Cloudflare Analytics 返回查询错误。")
    metrics = _parse_latest_metrics(payload)
    if metrics is None:
        raise ValueError("Cloudflare Analytics 响应缺少最新 R2 使用量指标。")
    return metrics


def refresh_cloudflare_usage(backend_id):
    current = MediaStorageBackend.objects.select_related("cloudflare_account_ref").get(pk=backend_id)
    metrics = fetch_cloudflare_usage(current)
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
