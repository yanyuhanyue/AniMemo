"""Repeated API/query probes over AniMemo's real Django/DRF code paths."""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict
from math import ceil
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from integrations.authentication import sign_hmac_request
from integrations.models import IntegrationConnection
from journal.auth_tokens import issue_token_pair
from journal.models import JournalEntry

from .contract import API_MEASURED_RUNS, API_WARMUP_RUNS, DEEP_DASHBOARD_PAGE, summarize_samples
from .seed import ADMIN_USERNAME, OWNER_USERNAME, SeedResult


NUMBER_RE = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")
STRING_RE = re.compile(r"'(?:''|[^'])*'")
SPACE_RE = re.compile(r"\s+")
EXPLAIN_PROBES = {
    "journal_page_48",
    "journal_facets",
    "plugin_marketplace",
    "staff_dashboard",
    "staff_plugin_review",
    "integration_events",
}
EXPECTED_STATUS_CODES = {200}


def normalize_sql(sql):
    normalized = STRING_RE.sub("?", str(sql))
    normalized = NUMBER_RE.sub("?", normalized)
    return SPACE_RE.sub(" ", normalized).strip()


def duplicate_query_summary(queries):
    counts = Counter(normalize_sql(item["sql"]) for item in queries)
    duplicates = [(sql, count) for sql, count in counts.items() if count > 1]
    duplicates.sort(key=lambda item: (-item[1], item[0]))
    return {
        "duplicate_executions": sum(count - 1 for _sql, count in duplicates),
        "shapes": [
            {"count": count, "sql": sql[:500]}
            for sql, count in duplicates[:10]
        ],
    }


def response_item_count(response):
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return len(data)
    if not isinstance(data, dict):
        return None
    for key in ("results", "plugins", "users", "bindings", "connections", "events", "submissions"):
        if isinstance(data.get(key), list):
            return len(data[key])
    return data.get("count") if isinstance(data.get("count"), int) else None


def _measure_once(request_callable):
    with CaptureQueriesContext(connection) as captured:
        started = time.perf_counter_ns()
        response = request_callable()
        content = response.content
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    queries = list(captured.captured_queries)
    duplicate_summary = duplicate_query_summary(queries)
    return {
        "latency_ms": round(elapsed_ms, 3),
        "query_count": len(queries),
        "duplicate_query_count": duplicate_summary["duplicate_executions"],
        "duplicate_query_shapes": duplicate_summary["shapes"],
        "response_bytes": len(content),
        "status_code": response.status_code,
        "item_count": response_item_count(response),
        "queries": queries,
    }


def _summarize_runs(samples, *, include_latency):
    status_codes = sorted({sample["status_code"] for sample in samples})
    summary = {
        "runs": len(samples),
        "status_codes": status_codes,
        "query_count": summarize_samples(sample["query_count"] for sample in samples),
        "duplicate_query_count": summarize_samples(
            sample["duplicate_query_count"] for sample in samples
        ),
        "response_bytes": summarize_samples(sample["response_bytes"] for sample in samples),
        "item_counts": sorted({sample["item_count"] for sample in samples if sample["item_count"] is not None}),
        "duplicate_query_shapes": samples[-1]["duplicate_query_shapes"],
    }
    if include_latency:
        summary["latency_ms"] = summarize_samples(sample["latency_ms"] for sample in samples)
    else:
        summary["latency_ms"] = "NOT AUTHORITATIVE — SQLite query-shape mode"
    return summary


def _explain_queries(queries):
    candidates = []
    seen = set()
    for item in sorted(queries, key=lambda row: float(row.get("time") or 0), reverse=True):
        sql = str(item.get("sql") or "").strip()
        normalized = normalize_sql(sql)
        if normalized in seen or not sql.lower().startswith(("select", "with")):
            continue
        seen.add(normalized)
        candidates.append(sql)
        if len(candidates) == 3:
            break
    plans = []
    with connection.cursor() as cursor:
        for sql in candidates:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
            plans.append({"sql": normalize_sql(sql)[:1000], "plan": cursor.fetchone()[0]})
    return plans


def _signed_events_request(client, integration):
    path = "/api/integrations/v1/events/?after=0&limit=50&wait=0"
    timestamp = str(int(time.time()))
    nonce = uuid4().hex
    signature = sign_hmac_request(
        integration.get_secret(),
        timestamp,
        nonce,
        "GET",
        path,
        b"",
    )
    return client.get(
        path,
        HTTP_X_ANIMEMO_KEY_ID=integration.key_id,
        HTTP_X_ANIMEMO_TIMESTAMP=timestamp,
        HTTP_X_ANIMEMO_NONCE=nonce,
        HTTP_X_ANIMEMO_SIGNATURE=signature,
    )


def build_probe_context(seed_result: SeedResult):
    user_model = get_user_model()
    owner = user_model.objects.get(username=OWNER_USERNAME)
    admin = user_model.objects.get(username=ADMIN_USERNAME)
    integration = IntegrationConnection.objects.get(pk=seed_result.integration_connection_id)
    owner_entry_count = JournalEntry.objects.filter(user=owner, deleted_at__isnull=True).count()
    total_pages = ceil(owner_entry_count / 48)
    client_defaults = {"SERVER_NAME": os.getenv("PERFORMANCE_SERVER_NAME", "localhost")}
    owner_client = APIClient(**client_defaults)
    _refresh, owner_access = issue_token_pair(owner)
    owner_client.credentials(HTTP_AUTHORIZATION=f"Bearer {owner_access}")
    admin_client = APIClient(**client_defaults)
    _admin_refresh, admin_access = issue_token_pair(admin)
    admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {admin_access}")
    anonymous_client = APIClient(**client_defaults)
    integration_client = APIClient(**client_defaults)

    probes = {
        "auth_session": lambda: owner_client.get(reverse("me")),
        "journal_page_1": lambda: owner_client.get(reverse("entry-list"), {"page": 1}),
        "journal_middle_page": lambda: owner_client.get(
            reverse("entry-list"), {"page": max(1, ceil(total_pages / 2))}
        ),
        "journal_filter": lambda: owner_client.get(
            reverse("entry-list"), {"status": "watching"}
        ),
        "journal_sort": lambda: owner_client.get(
            reverse("entry-list"), {"ordering": "-personal_score", "priority": 0}
        ),
        "journal_facets": lambda: owner_client.get(
            reverse("entry-list"), {"include_facets": 1}
        ),
        "journal_detail": lambda: owner_client.get(
            reverse("entry-detail", kwargs={"pk": seed_result.detail_entry_id})
        ),
        "watch_history_page_1": lambda: owner_client.get(
            reverse("watch-history-collection", kwargs={"entry_id": seed_result.history_entry_id}),
            {"page": 1, "page_size": 100},
        ),
        "watch_history_deep_page": lambda: owner_client.get(
            reverse("watch-history-collection", kwargs={"entry_id": seed_result.history_entry_id}),
            {"page": 5, "page_size": 100},
        ),
        "plugin_marketplace": lambda: anonymous_client.get(reverse("plugin-marketplace")),
        "plugin_installed": lambda: owner_client.get(reverse("plugin-installed")),
        "staff_dashboard": lambda: admin_client.get(reverse("staff-dashboard")),
        "staff_plugin_review": lambda: admin_client.get(reverse("staff-plugin-review-queue")),
        "integration_connections": lambda: owner_client.get("/api/integrations/v1/connections/"),
        "integration_bindings": lambda: owner_client.get("/api/integrations/v1/bindings/"),
        "integration_events": lambda: _signed_events_request(integration_client, integration),
    }
    if total_pages >= DEEP_DASHBOARD_PAGE:
        probes["journal_page_48"] = lambda: owner_client.get(
            reverse("entry-list"), {"page": DEEP_DASHBOARD_PAGE}
        )
    return probes, total_pages


def run_backend_probes(seed_result, *, authoritative, explain=False):
    if explain and connection.vendor != "postgresql":
        raise ValueError("EXPLAIN ANALYZE BUFFERS requires PostgreSQL")
    cache.clear()
    probes, total_pages = build_probe_context(seed_result)
    results = {}
    for name, request_callable in probes.items():
        for _index in range(API_WARMUP_RUNS):
            warmup = _measure_once(request_callable)
            if warmup["status_code"] not in EXPECTED_STATUS_CODES:
                raise RuntimeError(
                    f"{name} returned unexpected HTTP {warmup['status_code']} during warm-up"
                )
        measured = [_measure_once(request_callable) for _index in range(API_MEASURED_RUNS)]
        unexpected = sorted(
            {sample["status_code"] for sample in measured}
            - EXPECTED_STATUS_CODES
        )
        if unexpected:
            raise RuntimeError(f"{name} returned unexpected HTTP statuses: {unexpected}")
        result = _summarize_runs(measured, include_latency=authoritative)
        if explain and name in EXPLAIN_PROBES:
            result["explain_analyze_buffers"] = _explain_queries(measured[-1]["queries"])
        results[name] = result

    return {
        "schema_version": 1,
        "mode": "POSTGRESQL_AUTHORITATIVE" if authoritative else "SQLITE_QUERY_SHAPE_ONLY",
        "database": {
            "vendor": connection.vendor,
            "authoritative": authoritative,
            "explain_analyze_buffers": bool(explain),
        },
        "contract": {
            "warmup_runs": API_WARMUP_RUNS,
            "measured_runs": API_MEASURED_RUNS,
            "deep_dashboard_page": DEEP_DASHBOARD_PAGE,
        },
        "dataset": asdict(seed_result),
        "pagination": {
            "page_size": 48,
            "total_pages": total_pages,
            "page_48": "MEASURED" if total_pages >= DEEP_DASHBOARD_PAGE else "NOT APPLICABLE",
        },
        "probes": results,
    }


def write_probe_report(report, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
