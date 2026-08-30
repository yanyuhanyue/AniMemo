import re
from copy import deepcopy

from django.db import migrations

PLUGIN_RUNTIME_UNAVAILABLE = "plugin_runtime_unavailable"
PLUGIN_SCAN_FAILED = "plugin_scan_failed"
_REPORT_BOOLEAN_FIELDS = frozenset(
    {
        "contains_backend",
        "uses_external_network",
        "stores_personal_data",
        "accepts_file_uploads",
    }
)
_REPORT_INTEGER_FIELDS = frozenset(
    {"file_count", "package_size", "uncompressed_size"}
)
_PYTHON_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_HOOK_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_PERMISSION_CODE = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z][a-z0-9_-]*)+$"
)
_CSS_SELECTOR = re.compile(r"^[a-z0-9_.#*:\-\[\]=\"' ()>+~]+$")
_PERMISSION_ROLES = frozenset(
    {"reviewer", "user_manager", "operator", "administrator"}
)
_DANGEROUS_CALL = re.compile(
    r"^backend/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py:"
    r"[1-9][0-9]{0,6} uses (?:eval|exec|compile|__import__|os\.system|os\.popen)$"
)
_DANGEROUS_IMPORT = re.compile(
    r"^dangerous import: "
    r"(?:ctypes|django|multiprocessing|os|pathlib|resource|shutil|signal|socket|subprocess|sys)"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_BATCH_ERROR_CODE = "bangumi_lookup_unavailable"
_MAX_PUBLIC_INTEGER = 2_147_483_647
_MAX_PUBLIC_COLLECTION_ITEMS = 10_000
_MAX_SOURCE_NAMES = 8
_MISSING = object()
_BATCH_DIAGNOSTIC_KEYS = frozenset(
    {
        "detail",
        "error",
        "exception",
        "message",
        "sdk_error",
        "stderr",
        "stdout",
        "traceback",
    }
)
_BATCH_STATUSES = frozenset({"preview", "resolving", "ready", "imported"})
_BATCH_SUMMARY_FIELDS = frozenset(
    {
        "parsed",
        "included_records",
        "anime_groups",
        "excluded",
        "pending",
        "matched",
        "manual_review",
        "imported_entries",
        "imported_history_records",
        "selected_groups",
        "excluded_groups",
        "excluded_group_indices",
    }
)
_BATCH_RESULT_FIELDS = frozenset(
    {
        "batch_id",
        "imported_entries",
        "imported_records",
        "excluded_groups",
        "created",
        "updated",
        "skipped",
    }
)
_RESOLUTION_STATUSES = frozenset(
    {
        "pending",
        "matched",
        "season_mismatch",
        "no_result",
        "low_confidence",
        "ambiguous",
        "episode_mismatch",
        "network_error",
    }
)
_GROUP_TEXT_FIELDS = {
    "source_key": 4096,
    "source_title": 4096,
    "latest_watch_date": 32,
    "latest_watch_date_label": 128,
}
_RECORD_TEXT_FIELDS = {
    "title": 4096,
    "source_title": 4096,
    "watch_date": 32,
    "watch_date_label": 128,
    "brush_label": 32,
    "episode_claim_kind": 32,
    "exclusion_reason": 1000,
    "source_file": 255,
}
_RECORD_NULLABLE_TEXT_FIELDS = frozenset(
    {
        "watch_date",
        "watch_date_label",
        "episode_claim_kind",
        "exclusion_reason",
    }
)
_RESOLUTION_TEXT_FIELDS = {
    "title": 4096,
    "japanese_title": 4096,
    "air_date": 32,
    "studio": 2000,
    "description": 10_000,
    "poster_url": 1000,
    "source_url": 1000,
    "source_title": 4096,
}


def _stable_finding(value):
    if value == PLUGIN_SCAN_FAILED:
        return PLUGIN_SCAN_FAILED
    if isinstance(value, str) and (
        _DANGEROUS_CALL.fullmatch(value) or _DANGEROUS_IMPORT.fullmatch(value)
    ):
        return value
    return PLUGIN_SCAN_FAILED


def _stable_string_list(value, pattern, *, maximum_items=256, maximum_length=200):
    if not isinstance(value, list) or len(value) > maximum_items:
        return None
    stable = []
    for item in value:
        if (
            not isinstance(item, str)
            or len(item) > maximum_length
            or pattern.fullmatch(item) is None
        ):
            return None
        stable.append(item)
    return stable


def _stable_permissions(value):
    if not isinstance(value, list) or len(value) > 256:
        return None
    stable = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"code", "roles"}:
            return None
        code = item.get("code")
        roles = item.get("roles")
        if (
            not isinstance(code, str)
            or len(code) > 200
            or _PERMISSION_CODE.fullmatch(code) is None
            or not isinstance(roles, list)
            or len(roles) != len(set(roles))
            or not all(isinstance(role, str) and role in _PERMISSION_ROLES for role in roles)
        ):
            return None
        stable.append({"code": code, "roles": list(roles)})
    return stable


def _sanitize_report(report):
    if not isinstance(report, dict):
        return {"dangerous_findings": [PLUGIN_SCAN_FAILED]} if report else {}
    sanitized = {}
    for field in _REPORT_BOOLEAN_FIELDS:
        value = report.get(field)
        if isinstance(value, bool):
            sanitized[field] = value
    for field in _REPORT_INTEGER_FIELDS:
        value = report.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            sanitized[field] = value
    invalid_list = False
    list_specs = (
        ("hooks", _HOOK_NAME, 64, 80),
        ("backend_imports", _PYTHON_MODULE, 256, 200),
        ("css_global_selectors", _CSS_SELECTOR, 256, 200),
    )
    for field, pattern, maximum_items, maximum_length in list_specs:
        if field not in report:
            continue
        stable = _stable_string_list(
            report.get(field),
            pattern,
            maximum_items=maximum_items,
            maximum_length=maximum_length,
        )
        if stable is None:
            invalid_list = True
        else:
            sanitized[field] = stable
    if "permissions" in report:
        permissions = _stable_permissions(report.get("permissions"))
        if permissions is None:
            invalid_list = True
        else:
            sanitized["permissions"] = permissions
    findings = report.get("dangerous_findings")
    if isinstance(findings, list):
        sanitized["dangerous_findings"] = list(
            dict.fromkeys(_stable_finding(item) for item in findings)
        )
    elif findings:
        sanitized["dangerous_findings"] = [PLUGIN_SCAN_FAILED]
    elif "dangerous_findings" in report:
        sanitized["dangerous_findings"] = []
    if invalid_list:
        findings = sanitized.setdefault("dangerous_findings", [])
        if PLUGIN_SCAN_FAILED not in findings:
            findings.append(PLUGIN_SCAN_FAILED)
    return sanitized


def _sanitize_audit_payload(action, field, value):
    """Sanitize only known diagnostic positions without deleting plugin config."""
    if not isinstance(value, dict):
        return value
    sanitized = deepcopy(value)
    report_field = {
        ("plugin.version_upload", "after"): "scan",
        ("plugin.version_submit", "after"): "security_report",
    }.get((action, field))
    if report_field is not None and report_field in sanitized:
        sanitized[report_field] = _sanitize_report(sanitized[report_field])
    if action == "plugin.preview_create" and field == "metadata":
        sanitized.pop("path", None)
        sanitized.setdefault("stage", "preview_created")
    return sanitized


def _stable_text(value, maximum_length, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > maximum_length:
        return _MISSING
    return value


def _stable_integer(value, *, minimum=0, maximum=_MAX_PUBLIC_INTEGER, nullable=False):
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        return _MISSING
    return value


def _stable_bounded_string_list(value, *, maximum_items, maximum_length):
    if not isinstance(value, list) or len(value) > maximum_items:
        return _MISSING
    projected = []
    for item in value:
        stable = _stable_text(item, maximum_length)
        if stable is _MISSING:
            return _MISSING
        projected.append(stable)
    return projected


def _stable_integer_list(
    value,
    *,
    maximum_items,
    minimum=0,
    maximum=_MAX_PUBLIC_INTEGER,
    ordered_unique=False,
):
    if not isinstance(value, list) or len(value) > maximum_items:
        return _MISSING
    projected = []
    for item in value:
        stable = _stable_integer(item, minimum=minimum, maximum=maximum)
        if stable is _MISSING:
            return _MISSING
        projected.append(stable)
    if ordered_unique and projected != sorted(set(projected)):
        return _MISSING
    return projected


def _stable_episode_range(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        return _MISSING
    start = _stable_integer(value.get("start"), minimum=1, maximum=32767)
    end = _stable_integer(value.get("end"), minimum=1, maximum=32767)
    if start is _MISSING or end is _MISSING or end < start:
        return _MISSING
    return {"start": start, "end": end}


def _project_text_fields(value, fields, *, nullable_fields=frozenset()):
    if not isinstance(value, dict):
        return {}
    projected = {}
    for key, maximum_length in fields.items():
        if key not in value:
            continue
        stable = _stable_text(
            value[key],
            maximum_length,
            nullable=key in nullable_fields,
        )
        if stable is not _MISSING:
            projected[key] = stable
    return projected


def _sanitize_summary(value):
    summary = value if isinstance(value, dict) else {}
    projected = {}
    for key in _BATCH_SUMMARY_FIELDS - {"excluded_group_indices"}:
        if key not in summary:
            continue
        stable = _stable_integer(summary[key])
        if stable is not _MISSING:
            projected[key] = stable
    if "excluded_group_indices" in summary:
        indices = _stable_integer_list(
            summary["excluded_group_indices"],
            maximum_items=_MAX_PUBLIC_COLLECTION_ITEMS,
            ordered_unique=True,
        )
        if indices is not _MISSING:
            projected["excluded_group_indices"] = indices
    return projected


def _sanitize_result(value):
    result = value if isinstance(value, dict) else {}
    sanitized = {}
    if "batch_id" in result:
        batch_id = _stable_text(result["batch_id"], 64)
        if batch_id is not _MISSING:
            sanitized["batch_id"] = batch_id
    for key in _BATCH_RESULT_FIELDS - {"batch_id"}:
        if key not in result:
            continue
        stable = _stable_integer(result[key])
        if stable is not _MISSING:
            sanitized[key] = stable
    return sanitized


def _sanitize_resolution(value):
    if not isinstance(value, dict):
        return {
            "status": "network_error",
            "code": _BATCH_ERROR_CODE,
        }
    had_diagnostic = any(
        str(key).casefold() in _BATCH_DIAGNOSTIC_KEYS for key in value
    )
    status = value.get("status")
    if status not in _RESOLUTION_STATUSES or status == "network_error" or had_diagnostic:
        return {
            "status": "network_error",
            "code": _BATCH_ERROR_CODE,
        }
    sanitized = _project_text_fields(value, _RESOLUTION_TEXT_FIELDS)
    for key, minimum, maximum, nullable in (
        ("bangumi_id", 1, _MAX_PUBLIC_INTEGER, False),
        ("episodes", 0, 32767, True),
    ):
        if key not in value:
            continue
        stable = _stable_integer(
            value[key],
            minimum=minimum,
            maximum=maximum,
            nullable=nullable,
        )
        if stable is not _MISSING:
            sanitized[key] = stable
    for key in ("episode_exception", "manual_selection"):
        field = value.get(key, _MISSING)
        if isinstance(field, bool):
            sanitized[key] = field
    confidence = value.get("confidence", _MISSING)
    if (
        not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0 <= confidence <= 1
    ):
        sanitized["confidence"] = confidence
    if "tags" in value:
        tags = _stable_bounded_string_list(
            value["tags"],
            maximum_items=8,
            maximum_length=200,
        )
        if tags is not _MISSING:
            sanitized["tags"] = tags
    if "claimed_episodes" in value:
        claimed_episodes = _stable_integer_list(
            value["claimed_episodes"],
            maximum_items=512,
            minimum=1,
            maximum=32767,
            ordered_unique=True,
        )
        if claimed_episodes is not _MISSING:
            sanitized["claimed_episodes"] = claimed_episodes
    sanitized["status"] = status
    return sanitized


def _sanitize_record(value):
    if not isinstance(value, dict):
        return {}
    sanitized = _project_text_fields(
        value,
        _RECORD_TEXT_FIELDS,
        nullable_fields=_RECORD_NULLABLE_TEXT_FIELDS,
    )
    for key, minimum, maximum, nullable in (
        ("brush", 1, 32767, True),
        ("claimed_episodes", 1, 32767, True),
        ("source_line", 1, _MAX_PUBLIC_INTEGER, False),
    ):
        if key not in value:
            continue
        stable = _stable_integer(
            value[key],
            minimum=minimum,
            maximum=maximum,
            nullable=nullable,
        )
        if stable is not _MISSING:
            sanitized[key] = stable
    include = value.get("include", _MISSING)
    if isinstance(include, bool):
        sanitized["include"] = include
    if "notes" in value:
        notes = _stable_bounded_string_list(
            value["notes"],
            maximum_items=256,
            maximum_length=2000,
        )
        if notes is not _MISSING:
            sanitized["notes"] = notes
    if "episode_range" in value:
        episode_range = _stable_episode_range(value["episode_range"])
        if episode_range is not _MISSING:
            sanitized["episode_range"] = episode_range
    return sanitized


def _sanitize_group(value):
    group = value if isinstance(value, dict) else {}
    sanitized = _project_text_fields(group, _GROUP_TEXT_FIELDS)
    records = group.get("records")
    sanitized["records"] = [
        _sanitize_record(record)
        for record in records
    ] if isinstance(records, list) and len(records) <= _MAX_PUBLIC_COLLECTION_ITEMS else []
    sanitized["resolution"] = _sanitize_resolution(group.get("resolution"))
    return sanitized


def _sanitize_payload(value):
    payload = value if isinstance(value, dict) else {}
    groups = payload.get("groups")
    excluded = payload.get("excluded")
    return {
        "groups": [
            _sanitize_group(group)
            for group in groups
        ] if isinstance(groups, list) and len(groups) <= _MAX_PUBLIC_COLLECTION_ITEMS else [],
        "excluded": [
            _sanitize_record(record)
            for record in excluded
        ] if isinstance(excluded, list) and len(excluded) <= _MAX_PUBLIC_COLLECTION_ITEMS else [],
        "summary": _sanitize_summary(payload.get("summary")),
    }


def _sanitize_batch(value):
    if not isinstance(value, dict):
        return {}
    batch = {}
    for key, maximum_length, nullable in (
        ("id", 64, False),
        ("target_username", 150, False),
        ("target_email", 320, False),
        ("created_at", 64, True),
        ("updated_at", 64, True),
        ("imported_at", 64, True),
    ):
        if key not in value:
            continue
        stable = _stable_text(value[key], maximum_length, nullable=nullable)
        if stable is not _MISSING:
            batch[key] = stable
    status = value.get("status", _MISSING)
    if isinstance(status, str) and status in _BATCH_STATUSES:
        batch["status"] = status
    for key in ("created_by_id", "target_user_id"):
        if key not in value:
            continue
        stable = _stable_integer(value[key], minimum=1, nullable=True)
        if stable is not _MISSING:
            batch[key] = stable
    source_names = _stable_bounded_string_list(
        value.get("source_names"),
        maximum_items=_MAX_SOURCE_NAMES,
        maximum_length=255,
    )
    if source_names is not _MISSING:
        batch["source_names"] = source_names
    batch["summary"] = _sanitize_summary(value.get("summary"))
    batch["payload"] = _sanitize_payload(value.get("payload"))
    batch["result"] = _sanitize_result(value.get("result"))
    batch["error"] = (
        {"code": _BATCH_ERROR_CODE} if value.get("error") else {}
    )
    return batch


def redact_legacy_plugin_diagnostics(apps, _schema_editor):
    PluginDeployment = apps.get_model("plugin_host", "PluginDeployment")
    PluginData = apps.get_model("plugin_host", "PluginData")
    PluginSubmission = apps.get_model("plugin_host", "PluginSubmission")
    AdminAuditLog = apps.get_model("journal", "AdminAuditLog")

    PluginDeployment.objects.exclude(last_error="").update(
        last_error=PLUGIN_RUNTIME_UNAVAILABLE,
    )

    for submission in PluginSubmission.objects.exclude(security_report={}).iterator(
        chunk_size=500
    ):
        sanitized = _sanitize_report(submission.security_report)
        if sanitized != submission.security_report:
            submission.security_report = sanitized
            submission.save(update_fields=["security_report"])

    for row in PluginData.objects.filter(
        plugin__slug="watch-history-importer",
        namespace="batches",
    ).iterator(chunk_size=500):
        sanitized = _sanitize_batch(row.value)
        if sanitized != row.value:
            row.value = sanitized
            row.save(update_fields=["value", "updated_at"])

    for audit in AdminAuditLog.objects.filter(action__startswith="plugin.").iterator(
        chunk_size=500
    ):
        changed = []
        for field in ("before", "after", "metadata"):
            current = getattr(audit, field)
            sanitized = _sanitize_audit_payload(audit.action, field, current)
            if sanitized != current:
                setattr(audit, field, sanitized)
                changed.append(field)
        if changed:
            audit.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ("journal", "0006_external_provider_configuration"),
        ("plugin_host", "0003_add_plugin_data_retention_index"),
    ]

    operations = [
        migrations.RunPython(
            redact_legacy_plugin_diagnostics,
            migrations.RunPython.noop,
        ),
    ]
