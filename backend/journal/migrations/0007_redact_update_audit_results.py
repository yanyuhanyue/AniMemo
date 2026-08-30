import re
from datetime import datetime, timezone

from django.db import migrations

_ID = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_POLICY_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:beta|rc)\.[1-9][0-9]*)?$"
)
_CHANNELS = {"stable", "rc", "beta"}
_SOURCES = {"github", "official-mirror", "local-bundle"}
_DECISIONS = {"safe_switch", "application_rollback", "blocked", "unsafe_downgrade"}
_ROLLBACK_MODES = {"safe", "application", "blocked"}
_MIGRATION_POLICIES = {
    "none",
    "additive-backward-compatible",
    "breaking-blocked",
    "unknown",
}
_REASONS = {
    "database_contract_not_accepted",
    "configuration_contract_not_accepted",
    "enabled_plugin_sdk_not_supported",
    "breaking_migration_blocked",
    "Release compatibility could not be evaluated",
}
_LOCAL_DIGEST_FIELDS = {
    "transportIdentity",
    "payloadIdentity",
    "releaseAttestationIdentity",
    "trustProfileIdentity",
    "manifestIdentity",
    "deploymentContractIdentity",
    "apiDigest",
    "webDigest",
    "postgresDigest",
    "redisDigest",
}


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _timestamp(value):
    if type(value) is not str or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if (
        parsed.utcoffset() != timezone.utc.utcoffset(parsed)
        or parsed.isoformat().replace("+00:00", "Z") != value
    ):
        return None
    return value


def _identity(value):
    if type(value) is not dict or not (
        _matches(value.get("version"), _VERSION)
        and value.get("channel") in _CHANNELS
        and _matches(value.get("commit"), _COMMIT)
        and _matches(value.get("apiDigest"), _DIGEST)
        and _matches(value.get("webDigest"), _DIGEST)
    ):
        return None
    return {
        "version": value["version"],
        "channel": value["channel"],
        "commit": value["commit"],
        "apiDigest": value["apiDigest"],
        "webDigest": value["webDigest"],
    }


def _compatibility(value):
    if type(value) is not dict:
        return None
    reasons = value.get("reasons")
    if not (
        type(value.get("allowed")) is bool
        and value.get("decision") in _DECISIONS
        and value.get("rollbackMode") in _ROLLBACK_MODES
        and type(value.get("migrationRequired")) is bool
        and value.get("migrationPolicy") in _MIGRATION_POLICIES
        and type(reasons) is list
        and len(reasons) <= 16
        and all(reason in _REASONS for reason in reasons)
    ):
        return None
    return {
        "allowed": value["allowed"],
        "decision": value["decision"],
        "rollbackMode": value["rollbackMode"],
        "migrationRequired": value["migrationRequired"],
        "migrationPolicy": value["migrationPolicy"],
        "reasons": list(reasons),
    }


def _receipt(value):
    if type(value) is not dict or value.get("schema") != "animemo.release-execution-receipt/v1":
        return None
    digest_fields = {
        "publicationIdentity",
        "publicationExecutionReceiptIdentity",
        "signedClaimIdentity",
        "identity",
    }
    if any(not _matches(value.get(field), _DIGEST) for field in digest_fields):
        return None
    signed_at = _timestamp(value.get("signedAt"))
    if signed_at is None:
        return None
    return {
        "schema": value["schema"],
        "publicationIdentity": value["publicationIdentity"],
        "publicationExecutionReceiptIdentity": value[
            "publicationExecutionReceiptIdentity"
        ],
        "signedClaimIdentity": value["signedClaimIdentity"],
        "signedAt": signed_at,
        "identity": value["identity"],
    }


def _plan(value):
    if type(value) is not dict or value.get("source") not in _SOURCES:
        return {}
    source = value["source"]
    expires_at = _timestamp(value.get("expiresAt"))
    from_identity = _identity(value.get("from"))
    to_identity = _identity(value.get("to"))
    compatibility = _compatibility(value.get("compatibility"))
    if not (
        _matches(value.get("planId"), _ID)
        and expires_at is not None
        and from_identity is not None
        and to_identity is not None
        and compatibility is not None
        and value.get("affectedServices") == ["api", "web"]
        and value.get("databaseRollback") is False
        and _matches(value.get("transportPolicyIdentity"), _POLICY_IDENTITY)
        and _matches(value.get("verifiedReleaseIdentity"), _DIGEST)
    ):
        return {}
    result = {
        "planId": value["planId"],
        "expiresAt": expires_at,
        "from": from_identity,
        "to": to_identity,
        "compatibility": compatibility,
        "affectedServices": ["api", "web"],
        "databaseRollback": False,
        "source": source,
        "transportPolicyIdentity": value["transportPolicyIdentity"],
        "verifiedReleaseIdentity": value["verifiedReleaseIdentity"],
    }
    if source == "local-bundle":
        if any(
            not _matches(value.get(field), _DIGEST)
            for field in _LOCAL_DIGEST_FIELDS
        ):
            return {}
        profile_version = value.get("trustProfileVersion")
        receipt = _receipt(value.get("releaseExecutionReceipt"))
        if type(profile_version) is not int or profile_version < 1 or receipt is None:
            return {}
        for field in sorted(_LOCAL_DIGEST_FIELDS):
            result[field] = value[field]
        result["trustProfileVersion"] = profile_version
        result["releaseExecutionReceipt"] = receipt
    return result


def redact_update_audits(apps, schema_editor):
    del schema_editor
    AuditLog = apps.get_model("journal", "AdminAuditLog")
    for audit in AuditLog.objects.filter(
        action__in=[
            "system.update_plan",
            "system.update_apply",
            "system.update_rollback",
        ]
    ).iterator(chunk_size=500):
        fields = []
        if audit.action == "system.update_plan":
            projected = _plan(audit.after)
            if audit.after != projected:
                audit.after = projected
                fields.append("after")
        else:
            metadata = audit.metadata if type(audit.metadata) is dict else {}
            operation_id = metadata.get("operation_id")
            projected = (
                {"operation_id": operation_id}
                if _matches(operation_id, _ID)
                else {}
            )
            if audit.metadata != projected:
                audit.metadata = projected
                fields.append("metadata")
        if fields:
            audit.save(update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("journal", "0006_external_provider_configuration")]

    operations = [
        migrations.RunPython(redact_update_audits, migrations.RunPython.noop),
    ]
