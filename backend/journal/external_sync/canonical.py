from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import Decimal, InvalidOperation

SUPPORTED_FIELDS = ("watch_status", "personal_score", "review")
WATCH_STATUSES = frozenset(("planned", "watching", "completed", "on_hold", "dropped"))
MAX_SYNC_REVIEW_LENGTH = 10_000
MAX_BASELINE_BYTES = 32_768
MISSING = object()


def field_value(value=MISSING):
    if value is MISSING:
        return {"present": False, "value": None}
    return {"present": True, "value": value}


def canonical_score(value):
    try:
        score = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("personal_score must be a decimal between 0 and 10") from error
    if not score.is_finite() or score < 0 or score > 10:
        raise ValueError("personal_score must be a decimal between 0 and 10")
    normalized = score.normalize()
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    return format(normalized, "f")


def canonical_review(value):
    review = unicodedata.normalize("NFC", str(value))
    if len(review) > MAX_SYNC_REVIEW_LENGTH:
        raise ValueError("review exceeds the collection sync safety limit")
    return review


def canonical_snapshot(*, watch_status=MISSING, personal_score=MISSING, review=MISSING):
    if watch_status is not MISSING:
        watch_status = str(watch_status)
        if watch_status not in WATCH_STATUSES:
            raise ValueError("watch_status is not a supported canonical value")
    if personal_score is not MISSING:
        personal_score = canonical_score(personal_score)
    if review is not MISSING:
        review = canonical_review(review)
    return {
        "watch_status": field_value(watch_status),
        "personal_score": field_value(personal_score),
        "review": field_value(review),
    }


def local_snapshot(entry):
    return canonical_snapshot(
        watch_status=entry.watch_status,
        personal_score=entry.personal_score if entry.personal_score is not None else MISSING,
        review=entry.review,
    )


def validate_baselines(value):
    if not isinstance(value, dict):
        raise ValueError("baselines must be an object")
    unknown = set(value) - set(SUPPORTED_FIELDS)
    if unknown:
        raise ValueError("baselines contains unsupported fields")
    for field, item in value.items():
        if not isinstance(item, dict) or set(item) != {"present", "value"}:
            raise ValueError(f"{field} baseline must contain only present and value")
        if not isinstance(item["present"], bool):
            raise ValueError(f"{field} baseline present must be boolean")
        if not item["present"]:
            if item["value"] is not None:
                raise ValueError(f"{field} missing baseline value must be null")
            continue
        if field == "watch_status":
            if item["value"] not in WATCH_STATUSES:
                raise ValueError("watch_status baseline is invalid")
        elif field == "personal_score":
            if item["value"] != canonical_score(item["value"]):
                raise ValueError("personal_score baseline is not canonical")
        elif field == "review":
            if not isinstance(item["value"], str) or item["value"] != canonical_review(item["value"]):
                raise ValueError("review baseline is not canonical")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BASELINE_BYTES:
        raise ValueError("baselines exceeds the storage safety limit")
    return value


def fingerprint(snapshot):
    payload = {
        "schema_version": 1,
        "fields": snapshot,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
