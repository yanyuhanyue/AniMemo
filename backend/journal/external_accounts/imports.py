from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from journal.external_media.errors import ExternalMediaError
from journal.external_media.services import (
    bind_prepared_external_identity,
    create_prepared_identity,
    lock_identity_owner,
    prepare_identity,
)
from journal.domain_services import JournalEntryService
from journal.models import ExternalImportSession, ExternalMediaIdentity, JournalEntry

from .connections import connection_token_for_use, mark_needs_reauthorization
from .errors import ExternalAccountError, import_item_invalid, import_preview_expired
from .matching import annotate_matches, bind_targets
from .registry import get_account_provider


IMPORT_MODES = {"CREATE_NEW", "BIND_EXISTING", "IMPORT_SAFE_USER_FIELDS", "SKIP"}
IMPORTABLE_USER_FIELDS = {"personal_score", "watch_status", "review"}


def create_import_preview(*, user, provider_slug):
    connection, provider, token = connection_token_for_use(user=user, provider_slug=provider_slug)
    try:
        remote_rows = provider.get_collections(
            token,
            connection.external_username,
            max_items=int(provider.import_max_items()),
        )
    except ExternalAccountError as error:
        if error.detail.get("code") == "external_account_token_invalid":
            mark_needs_reauthorization(connection)
        raise
    now = timezone.now()
    rows = annotate_matches(user, provider.slug, remote_rows)
    ExternalImportSession.objects.filter(expires_at__lt=now).delete()
    session = ExternalImportSession.objects.create(
        user=user,
        provider=provider.slug,
        snapshot_schema_version=1,
        snapshot=rows,
        expires_at=now + timedelta(seconds=int(settings.EXTERNAL_IMPORT_PREVIEW_TTL_SECONDS)),
    )
    type(connection).objects.filter(pk=connection.pk).update(last_used_at=now, updated_at=now)
    return session


def get_import_preview(*, user, provider_slug, preview_id, page=1, page_size=24, filter_value="all", query=""):
    provider = get_account_provider(provider_slug)
    session = ExternalImportSession.objects.filter(pk=preview_id, user=user, provider=provider.slug).first()
    if session is None or session.expires_at <= timezone.now() or session.applied_at is not None:
        raise import_preview_expired()
    if session.snapshot_schema_version != 1 or not isinstance(session.snapshot, list):
        raise import_preview_expired()
    rows = list(session.snapshot)
    filter_value = str(filter_value or "all").strip().lower()
    if filter_value in {"planned", "watching", "completed", "on_hold", "dropped"}:
        rows = [row for row in rows if row.get("remote_status") == filter_value]
    elif filter_value == "conflict":
        rows = [row for row in rows if row.get("conflicts") or row.get("match_state") == "possible_local_match"]
    elif filter_value == "existing":
        rows = [row for row in rows if row.get("match_state") == "already_bound"]
    query = str(query or "").strip().casefold()[:100]
    if query:
        rows = [
            row for row in rows
            if query in str(row.get("title") or "").casefold()
            or query in str(row.get("japanese_title") or "").casefold()
        ]
    try:
        page_size = min(50, max(1, int(page_size or 24)))
        page = max(1, int(page or 1))
    except (TypeError, ValueError) as error:
        raise import_item_invalid("分页参数无效。") from error
    total = len(rows)
    start = (page - 1) * page_size
    summary = {
        "remote_count": len(session.snapshot),
        "already_bound": sum(1 for row in session.snapshot if row.get("match_state") == "already_bound"),
        "possible_duplicates": sum(1 for row in session.snapshot if row.get("match_state") == "possible_local_match"),
        "conflicts": sum(1 for row in session.snapshot if row.get("conflicts")),
    }
    return {
        "preview_id": str(session.pk),
        "schema_version": session.snapshot_schema_version,
        "expires_at": session.expires_at,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
        "results": rows[start:start + page_size],
        "summary": summary,
        "bind_targets": bind_targets(user, provider.slug),
    }


def _airing_period(metadata):
    parts = str(metadata.get("air_date") or "")[:32].split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and 1 <= int(parts[1]) <= 12:
        return f"{parts[0]}-{int(parts[1])}"
    return ""


def _create_imported_entry(user, prepared, row):
    from journal.serializers_entries import JournalEntrySerializer

    metadata = prepared.metadata
    rating = row.get("remote_rating")
    values = {
        "title": str(metadata.get("title") or row.get("title") or f"{prepared.provider} #{prepared.external_id}")[:200],
        "japanese_title": str(metadata.get("japanese_title") or "")[:200],
        "airing_period": _airing_period(metadata)[:50],
        "studio": str(metadata.get("studio") or "")[:120],
        "episodes": str(metadata.get("episodes") or "")[:30],
        "description": str(metadata.get("summary") or "")[:10000],
        "poster_url": str(metadata.get("poster_url") or "")[:1000],
        "tags": [str(tag)[:100] for tag in (metadata.get("tags") or [])[:30]],
        "personal_score": Decimal(str(rating)) if rating is not None else None,
        "watch_status": row.get("remote_status") or JournalEntry.WatchStatus.PLANNED,
        "review": str(row.get("remote_comment") or "")[:10000],
    }
    dto = JournalEntryService(user).create_from_fields(
        values,
        serializer_class=JournalEntrySerializer,
        source="external-account-import",
        allowed_fields=set(values),
    )
    entry = JournalEntry.objects.get(pk=dto["entry_id"], user=user)
    identity = create_prepared_identity(entry, prepared)
    _record_import_provenance(identity, row)
    return entry


def _record_import_provenance(identity, row):
    metadata = dict(identity.metadata) if isinstance(identity.metadata, dict) else {}
    metadata["import_provenance"] = {
        "source": identity.provider,
        "imported_at": timezone.now().isoformat(),
        "collection_status": row.get("remote_status"),
        "collection_updated_at": row.get("remote_updated_at") or "",
    }
    identity.metadata = metadata
    identity.save(update_fields=["metadata", "updated_at"])


def _apply_explicit_user_fields(entry, row, fields):
    from journal.serializers_entries import JournalEntrySerializer

    values = {}
    if "personal_score" in fields and row.get("remote_rating") is not None:
        values["personal_score"] = Decimal(str(row["remote_rating"]))
    if "watch_status" in fields and row.get("remote_status") in JournalEntry.WatchStatus.values:
        values["watch_status"] = row["remote_status"]
    if "review" in fields and row.get("remote_comment"):
        values["review"] = str(row["remote_comment"])[:10000]
    if values:
        JournalEntryService(entry.user).update_from_fields(
            entry.pk,
            values,
            serializer_class=JournalEntrySerializer,
            source="external-account-import",
            allowed_fields=set(values),
        )
    return list(values)


def _apply_import_item(*, user, provider_slug, row, action, prepared):
    mode = action["mode"]
    fields = action["apply_fields"]
    if mode == "SKIP":
        return {"external_id": row["external_id"], "status": "skipped"}
    if mode == "CREATE_NEW":
        if prepared is None:
            raise import_item_invalid("无法读取该作品的权威资料。")
        entry = _create_imported_entry(user, prepared, row)
        return {"external_id": row["external_id"], "status": "created", "entry_id": entry.pk}
    if mode == "BIND_EXISTING":
        if prepared is None:
            raise import_item_invalid("无法读取该作品的权威资料。")
        entry = JournalEntry.objects.filter(
            pk=action.get("local_entry_id"),
            user=user,
            deleted_at__isnull=True,
        ).first()
        if entry is None:
            raise import_item_invalid("要绑定的本地记录不存在。")
        identity = bind_prepared_external_identity(entry=entry, user=user, prepared=prepared)
        _record_import_provenance(identity, row)
        changed = _apply_explicit_user_fields(entry, row, fields)
        return {"external_id": row["external_id"], "status": "bound", "entry_id": entry.pk, "updated_fields": changed}
    identity = ExternalMediaIdentity.objects.select_related("entry").filter(
        entry__user=user,
        entry__deleted_at__isnull=True,
        provider=provider_slug,
        external_id=row["external_id"],
    ).first()
    if identity is None:
        raise import_item_invalid("该作品当前未绑定，无法导入用户字段。")
    changed = _apply_explicit_user_fields(identity.entry, row, fields)
    return {
        "external_id": row["external_id"],
        "status": "updated" if changed else "skipped",
        "entry_id": identity.entry_id,
        "updated_fields": changed,
    }


def _normalize_actions(items):
    if not isinstance(items, list) or not items:
        raise import_item_invalid("请选择至少一个导入项目。")
    if len(items) > int(settings.EXTERNAL_IMPORT_APPLY_MAX_ITEMS):
        raise import_item_invalid("单次导入项目过多，请分批处理。")
    actions = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            actions.append({
                "external_id": "", "mode": "SKIP", "local_entry_id": None,
                "apply_fields": set(), "error": "import_item_invalid",
            })
            continue
        external_id = str(item.get("external_id") or "").strip()
        mode = str(item.get("mode") or "SKIP").strip().upper()
        fields = item.get("apply_fields") or []
        local_entry_id = item.get("local_entry_id")
        error = None
        if not external_id or len(external_id) > 200 or external_id in seen or mode not in IMPORT_MODES:
            error = "import_item_invalid"
        elif not isinstance(fields, list) or any(field not in IMPORTABLE_USER_FIELDS for field in fields):
            error = "import_item_invalid"
        elif mode == "BIND_EXISTING":
            try:
                local_entry_id = int(local_entry_id)
                if local_entry_id < 1:
                    raise ValueError
            except (TypeError, ValueError):
                error = "import_item_invalid"
                local_entry_id = None
        else:
            local_entry_id = None
        if external_id:
            seen.add(external_id)
        actions.append({
            "external_id": external_id,
            "mode": mode,
            "local_entry_id": local_entry_id,
            "apply_fields": set(fields) if isinstance(fields, list) else set(),
            "error": error,
        })
    return actions


def apply_import_preview(*, user, provider_slug, preview_id, items):
    provider = get_account_provider(provider_slug)
    actions = _normalize_actions(items)
    initial = ExternalImportSession.objects.filter(pk=preview_id, user=user, provider=provider.slug).first()
    if initial is None or initial.expires_at <= timezone.now() or initial.snapshot_schema_version != 1:
        raise import_preview_expired()
    if initial.applied_at is not None:
        return initial.result
    rows_by_id = {
        str(row.get("external_id")): row
        for row in initial.snapshot
        if isinstance(row, dict) and row.get("external_id")
    }
    prepared = {}
    preparation_errors = {}
    for action in actions:
        if action["error"]:
            continue
        row = rows_by_id.get(action["external_id"])
        if row is None:
            preparation_errors[action["external_id"]] = "import_item_invalid"
            continue
        if action["mode"] not in {"CREATE_NEW", "BIND_EXISTING"}:
            continue
        try:
            prepared[action["external_id"]] = prepare_identity(provider.slug, action["external_id"])
        except ExternalMediaError as error:
            preparation_errors[action["external_id"]] = str(error.detail.get("code") or "provider_unavailable")

    with transaction.atomic():
        session = ExternalImportSession.objects.select_for_update().filter(
            pk=preview_id,
            user=user,
            provider=provider.slug,
        ).first()
        if session is None or session.expires_at <= timezone.now() or session.snapshot_schema_version != 1:
            raise import_preview_expired()
        if session.applied_at is not None:
            return session.result
        lock_identity_owner(user)
        results = []
        for action in actions:
            external_id = action["external_id"]
            row = rows_by_id.get(external_id)
            if action["error"] or row is None or external_id in preparation_errors:
                results.append({
                    "external_id": external_id,
                    "status": "failed",
                    "code": action["error"] or preparation_errors.get(external_id, "import_item_invalid"),
                })
                continue
            try:
                with transaction.atomic():
                    results.append(_apply_import_item(
                        user=user,
                        provider_slug=provider.slug,
                        row=row,
                        action=action,
                        prepared=prepared.get(external_id),
                    ))
            except (ExternalAccountError, ExternalMediaError, IntegrityError) as error:
                detail = getattr(error, "detail", {})
                results.append({
                    "external_id": external_id,
                    "status": "conflict" if getattr(error, "status_code", None) == 409 else "failed",
                    "code": str(detail.get("code") or "import_conflict"),
                })
        counts = {
            name: sum(1 for item in results if item["status"] == name)
            for name in ("created", "bound", "updated", "skipped", "conflict", "failed")
        }
        result = {"preview_id": str(session.pk), "results": results, "counts": counts}
        session.snapshot = []
        session.result = result
        session.applied_at = timezone.now()
        session.save(update_fields=["snapshot", "result", "applied_at", "updated_at"])
        return result
