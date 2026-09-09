"""Read-only media inventory and explicitly invoked, idempotent backfill.

Legacy bytes and business fields are never deleted or rewritten. Ambiguous
objects stay outside the cleanup gate; completing one batch is not completion
of the inventory. The same inspector is used by Restore's integrity gate.
"""

import logging

from django.db import connection, transaction
from django.db.models.signals import post_save, pre_save
from django.utils import timezone
from site_config.media_storage.identity import (
    absolute_public_url,
    resolve_url_candidates,
    url_identity_digest,
)
from site_config.media_storage.pool import StoragePoolService
from site_config.models import MediaObject, MediaStorageBackend, MediaWriteReservation

from .media_references import (
    SLOTS,
    MediaAdmissionError,
    file_name,
    lock_media_owner,
    managed_id,
    owner_evidence,
    poster_usage,
    trustworthy_size,
    verify_new_holding,
)
from .models import Column, JournalEntry, JournalMediaReference, UserSettings


def _bytes_issue(media):
    if trustworthy_size(media) is None:
        return "UNKNOWN_USAGE"
    if media.lifecycle == MediaObject.Lifecycle.DELETING:
        return "MISSING_OR_INVALID_BYTES"
    if media.storage_backend.backend_type != MediaStorageBackend.BackendType.LOCAL:
        return "UNVERIFIED_REMOTE_BYTES"
    try:
        verify_new_holding(media)
    except MediaAdmissionError:
        return "MISSING_OR_INVALID_BYTES"
    return None


def expected_holding(entry, slot, byte_issues=None, *, read_source=None):
    """Return an exact existing business association, or a diagnostic."""
    value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
    if not value:
        return None, None, []
    if slot == "poster_file":
        media_id = managed_id(value)
        media = ((MediaObject.objects.filter(pk=media_id).first() if media_id else None)
                 if read_source is None else read_source.media_for_id(media_id))
        if media is None:
            return None, "MISSING_MEDIA_ROW", []
        issue = _holding_bytes_issue(media, byte_issues, read_source)
        # An existing direct field already proves its association. Remote byte
        # health remains unverified; it cannot establish a new URL holding.
        return media, None if issue == "UNVERIFIED_REMOTE_BYTES" else issue, [media.pk]
    candidates = resolve_url_candidates(value) if read_source is None else read_source.resolve_candidates(value)
    if not candidates:
        return None, "LINK_ONLY", []
    ids = [media.pk for media in candidates]
    if len(candidates) != 1:
        return None, "AMBIGUOUS_URL", ids
    media = candidates[0]
    owners = owner_evidence(media) if read_source is None else read_source.owners_for(media)
    if not owners:
        return None, "OWNER_UNPROVEN", ids
    if len(owners) != 1:
        return None, "OWNER_CONFLICT", ids
    if owners != {entry.user_id}:
        return None, "LINK_ONLY", []
    issue = _holding_bytes_issue(media, byte_issues, read_source)
    return (None, issue, ids) if issue else (media, None, ids)


def _holding_bytes_issue(media, byte_issues, read_source):
    if read_source is not None:
        from .media_inventory_context import ReadContextIncomplete

        read_source.check()
        if byte_issues is None or media.pk not in byte_issues:
            raise ReadContextIncomplete("Inventory byte verification domain is incomplete")
        return byte_issues[media.pk]
    return byte_issues.get(media.pk) if byte_issues is not None else _bytes_issue(media)


def inspect_media_references(*, use_read_context=True):
    """Never writes DB/files, changes ownership, or calls a remote adapter."""
    # Do not change isolation inside a caller's transaction (including manual
    # autocommit=False), and retain the existing live path on other databases.
    if connection.vendor != "postgresql" or connection.in_atomic_block or not connection.get_autocommit():
        return _inspect_media_references()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if not use_read_context:
            return _inspect_media_references()
        from .media_inventory_context import InventoryReadContext, ReadContextIncomplete

        context = None
        try:
            context = InventoryReadContext()
            result = _inspect_media_references(context)
            context.check()
            return result
        except ReadContextIncomplete:
            # Preserve real diagnostic rows and the CLI schema, but never turn
            # an incomplete or changed context into an apparently READY report.
            logging.getLogger(__name__).warning("media_inventory_read_context_incomplete")
            result = _inspect_media_references()
            result["status"] = "BLOCKED"
            return result
        finally:
            if context is not None:
                context.close()


def _inspect_media_references(read_source=None):
    objects, byte_issues = {}, {}
    media_rows = (MediaObject.objects.select_related("storage_backend").order_by("pk")
                  if read_source is None else read_source.objects)
    for media in media_rows:
        issue = _bytes_issue(media)
        byte_issues[media.pk] = issue
        objects[media.pk] = {
            "media_id": str(media.pk), "storage_backend_id": str(media.storage_backend_id),
            "object_key": media.object_key, "size_bytes": trustworthy_size(media),
            "upload_owner_id": media.upload_owner_id,
            "inventory_complete": media.reference_inventory_complete,
            "lifecycle": media.lifecycle,
            "byte_verification": issue or "VERIFIED_LOCAL_BYTES",
            "issues": [issue] if issue and issue != "UNVERIFIED_REMOTE_BYTES" else [],
        }
    expected = {}
    slots = []
    entry_rows = (JournalEntry.objects.all().order_by("pk").iterator(chunk_size=200)
                  if read_source is None else read_source.entries())
    for entry in entry_rows:
        for slot in SLOTS:
            value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
            if not value:
                continue
            media, issue, candidates = expected_holding(entry, slot, byte_issues, read_source=read_source)
            row = {"entry_id": entry.pk, "owner_id": entry.user_id, "slot": slot,
                   "value": value, "soft_deleted": entry.deleted_at is not None,
                   "media_id": str(media.pk) if media else None,
                   "issue": issue, "candidate_ids": [str(pk) for pk in candidates]}
            slots.append(row)
            if media:
                expected[(entry.pk, slot)] = (entry.user_id, media.pk, value)
            if issue and issue != "LINK_ONLY":
                for media_id in candidates:
                    objects[media_id]["issues"].append(issue)
    actual = {}
    reference_rows = (JournalMediaReference.objects.all().order_by("pk")
                      if read_source is None else read_source.references)
    for ref in reference_rows:
        key = (ref.entry_id, ref.slot)
        actual[key] = (ref.owner_id, ref.media_id, ref.value)
        if expected.get(key) != actual[key] and ref.media_id in objects:
            objects[ref.media_id]["issues"].append("REFERENCE_MISMATCH")
    for key, value in expected.items():
        if actual.get(key) != value:
            objects[value[1]]["issues"].append("REFERENCE_MISSING" if key not in actual else "REFERENCE_MISMATCH")
    # Other existing image roles are not charged, but missing role bytes or a
    # malformed identity must remain visible in Restore/inventory diagnostics.
    roles = []
    from site_config.models import SiteSettings

    role_rows = ((model._meta.label, pk, field, value)
                 for model, field in ((UserSettings, "avatar"), (Column, "cover"), (SiteSettings, "site_avatar"))
                 for pk, value in model.objects.exclude(**{field: ""}).exclude(**{field: None}).values_list("pk", field)) if read_source is None else read_source.roles
    for model_label, pk, field, value in role_rows:
        media_id = managed_id(value)
        issue = "MISSING_MEDIA_ROW" if media_id not in objects else None
        roles.append({"model": model_label, "pk": pk, "field": field, "media_id": str(media_id) if media_id else None, "issue": issue})
    object_rows = list(objects.values())
    for row in object_rows:
        row["issues"] = sorted(set(row["issues"]))
    unresolved_writes = (list(MediaWriteReservation.objects.filter(status__in=["pending", "cleanup_ready", "cleanup_failed"]).order_by("pk").values("id", "status", "size_bytes", "cleanup_error"))
                         if read_source is None else read_source.unresolved_writes)
    for row in unresolved_writes:
        row["id"] = str(row["id"])
    issues = [row for row in slots if row["issue"] not in {None, "LINK_ONLY"}]
    ready = not issues and not any(row["issues"] or not row["inventory_complete"] for row in object_rows) and not any(row["issue"] for row in roles) and not unresolved_writes
    return {
        "status": "READY" if ready else "BLOCKED", "read_only": True,
        "objects": object_rows, "slots": slots, "other_roles": roles,
        "unresolved_writes": unresolved_writes,
        "usage": [{"owner_id": owner_id, "status": (usage := poster_usage(owner_id)).status,
                   "known_bytes": usage.known_bytes, "unknown_slots": list(usage.unknown_slots)}
                  for owner_id in JournalEntry.objects.order_by("user_id").values_list("user_id", flat=True).distinct()] if read_source is None else read_source.usage_rows(),
    }


def backfill_media_references(*, after_entry=0, limit=200):
    _validate_backfill_limits(after_entry, limit)
    batch = _backfill_entry_batch(after_entry, limit)
    if batch["more"]:
        _close_inventory_gates()
    return _finish_backfill(batch)


def backfill_all_media_references(*, after_entry=0, limit=200):
    """Repair bounded entry batches and return the final complete inventory."""
    _validate_backfill_limits(after_entry, limit)
    # Closing all observed media first is safe only when those UPDATE locks
    # release before owner admission. Preserve the established single-batch
    # path under a caller's transaction, including its rollback and lock order.
    caller_transaction = connection.in_atomic_block or not connection.get_autocommit()
    step = backfill_media_references if caller_transaction else _backfill_entry_batch
    if not caller_transaction:
        _close_inventory_gates()
    result = step(after_entry=after_entry, limit=limit)
    # Keep the command's original bounded remaining-snapshot loop. Compatible
    # concurrent writers maintain holdings; new work cannot extend it forever.
    remaining = JournalEntry.objects.filter(pk__gt=result["next_after_entry"]).count()
    for _ in range(remaining // limit + 1):
        if not result["more"]:
            break
        cursor = result["next_after_entry"]
        result = step(after_entry=cursor, limit=limit)
        if result["more"] and result["next_after_entry"] <= cursor:
            raise ValueError("Backfill cursor did not advance")
    return result if caller_transaction else _finish_backfill(result)


def _validate_backfill_limits(after_entry, limit):
    if after_entry < 0 or not 1 <= limit <= 1000:
        raise ValueError("Backfill requires after_entry >= 0 and 1 <= limit <= 1000")


def _backfill_entry_batch(after_entry, limit):
    entry_ids = list(JournalEntry.objects.filter(pk__gt=after_entry).order_by("pk").values_list("pk", flat=True)[:limit])
    for entry_id in entry_ids:
        owner_id = JournalEntry.objects.filter(pk=entry_id).values_list("user_id", flat=True).first()
        if owner_id is None:
            continue
        with transaction.atomic():
            lock_media_owner(owner_id)
            entry = (JournalEntry.objects.select_for_update()
                     .only("id", "user_id", "poster_file", "custom_poster_url", "deleted_at")
                     .filter(pk=entry_id, user_id=owner_id).first())
            if entry is None:
                continue
            proposals = {}
            ids = set()
            for slot in SLOTS:
                if slot == "poster_file":
                    # The persisted direct field already proves association,
                    # including when its bytes need repair. Only existence is
                    # needed here; the fresh lifecycle lock below is authority.
                    direct_id = managed_id(file_name(entry))
                    direct_id = (MediaObject.objects.filter(pk=direct_id).values_list("pk", flat=True).first()
                                 if direct_id else None)
                    if direct_id is not None:
                        ids.add(direct_id)
                        proposals[slot] = direct_id
                    continue
                media, issue, candidates = expected_holding(entry, slot)
                ids.update(candidates)
                if media is not None and issue is None:
                    proposals[slot] = media.pk
            locked = {media.pk: media for media in MediaObject.objects.select_related("storage_backend")
                      .select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk")}
            for slot, media_id in proposals.items():
                media = locked.get(media_id)
                if media is None or media.lifecycle == MediaObject.Lifecycle.DELETING:
                    continue
                if slot != "poster_file":
                    # Re-evaluate URL identity, ownership and actual bytes after
                    # lifecycle serialization. Direct identity is already the
                    # same locked row and must retain even damaged bytes.
                    verified, issue, _candidates = expected_holding(entry, slot)
                    if verified is None or verified.pk != media_id or issue:
                        continue
                value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
                _insert_backfill_holding(entry, slot, owner_id, media, value)
                if media.upload_owner_id is None and owner_evidence(media) == {owner_id}:
                    media.upload_owner_id = owner_id
                    media.save(update_fields=["upload_owner"])
                if not media.public_url_snapshot:
                    # Always build from server configuration, never adopt the
                    # historical client URL as a new authority statement.
                    url = absolute_public_url(StoragePoolService.adapter_for(media.storage_backend).url(media.object_key))
                    media.public_url_snapshot = url
                    media.public_url_identity = url_identity_digest(url)
                    media.save(update_fields=["public_url_snapshot", "public_url_identity"])
    last = entry_ids[-1] if entry_ids else after_entry
    more = JournalEntry.objects.filter(pk__gt=last).exists()
    return {"processed_entries": len(entry_ids), "next_after_entry": last, "more": more}


def _insert_backfill_holding(entry, slot, owner_id, media, value):
    # The caller owns owner -> entry -> media locks. Preserve ORM save hooks
    # when installed, and retain the established path on other databases.
    if (connection.vendor != "postgresql" or pre_save.has_listeners(JournalMediaReference)
            or post_save.has_listeners(JournalMediaReference)):
        JournalMediaReference.objects.get_or_create(
            entry=entry, slot=slot,
            defaults={"owner_id": owner_id, "media": media, "value": value},
        )
        return
    quote = connection.ops.quote_name
    table = quote(JournalMediaReference._meta.db_table)
    columns = ", ".join(quote(name) for name in
                        ("entry_id", "slot", "owner_id", "media_id", "value", "created_at"))
    # Suppress only the existing entry/slot holding. A stale mismatched row is
    # never overwritten; final inventory reports it. PK, CHECK and FK failures
    # still abort the caller's transaction instead of being silently ignored.
    constraint = quote("journal_media_entry_slot_uq")
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} ({columns}) VALUES (%s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT ON CONSTRAINT {constraint} DO NOTHING",
            [entry.pk, slot, owner_id, media.pk, value, timezone.now()],
        )


def _close_inventory_gates():
    # Each conditional UPDATE rechecks DELETING after a competing claim. No
    # byte scan runs while this query holds its lifecycle lock. A caller's
    # outer transaction still owns its natural rollback and retained locks.
    for media_id in MediaObject.objects.order_by("pk").values_list("pk", flat=True).iterator(chunk_size=200):
        MediaObject.objects.filter(pk=media_id).exclude(lifecycle=MediaObject.Lifecycle.DELETING).update(
            reference_inventory_complete=False, inventory_error="BACKFILL_INCOMPLETE",
        )


def _finish_backfill(batch):
    if batch["more"]:
        result = inspect_media_references()
        result.update(batch, status="INCOMPLETE", read_only=False)
        return result
    inventory = inspect_media_references()
    candidate_entries_by_media = {}
    for slot in inventory["slots"]:
        for media_id in slot["candidate_ids"]:
            candidate_entries_by_media.setdefault(media_id, set()).add(slot["entry_id"])
    # Recheck each object under its lifecycle lock before opening its cleanup
    # gate. New writes take the same object lock and maintain references.
    for row in inventory["objects"]:
        with transaction.atomic():
            media = (MediaObject.objects.select_related("storage_backend")
                     .select_for_update(of=("self",)).filter(pk=row["media_id"]).first())
            if media is None:
                continue
            # A committed cleanup claim has already passed this gate and denies
            # new attachments. Re-running backfill must not erase the proof
            # needed to finish an interrupted deletion.
            if media.lifecycle == MediaObject.Lifecycle.DELETING:
                continue
            current = inspect_object_references(media, candidate_entries_by_media.get(str(media.pk), ()))
            complete = not current
            media.reference_inventory_complete = complete
            media.inventory_error = current[0] if current else ""
            media.save(update_fields=["reference_inventory_complete", "inventory_error"])
    result = inspect_media_references()
    result.update(batch, read_only=False)
    return result


def inspect_object_references(media, candidate_entries=()):
    """Lifecycle-lock-local check; read business rows without reversing locks."""
    issues = []
    bytes_issue = _bytes_issue(media)
    if bytes_issue and bytes_issue != "UNVERIFIED_REMOTE_BYTES":
        issues.append(bytes_issue)
    actual = {(ref.entry_id, ref.slot): ref for ref in media.journal_references.only(
        "entry_id", "owner_id", "slot", "media_id", "value")}
    # The initial full inventory finds legacy URL candidates. Current direct
    # fields and held rows cover writes committed since that snapshot; a new
    # owner holding cannot pass this same object lock without creating its row.
    entry_ids = set(candidate_entries) | {entry_id for entry_id, _slot in actual}
    entry_ids.update(JournalEntry.objects.filter(poster_file=media.reference_name).values_list("pk", flat=True))
    for entry in (JournalEntry.objects.filter(pk__in=entry_ids)
                  .only("id", "user_id", "poster_file", "custom_poster_url", "deleted_at").order_by("pk")):
        for slot in SLOTS:
            value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
            if not value:
                continue
            if slot == "poster_file":
                if managed_id(value) != media.pk:
                    continue
                # This exact media row is already freshly lifecycle-locked;
                # a second SELECT cannot strengthen its persisted identity.
                target, ids = media, [media.pk]
                issue = None if bytes_issue == "UNVERIFIED_REMOTE_BYTES" else bytes_issue
            else:
                target, issue, ids = expected_holding(entry, slot, {media.pk: bytes_issue})
            if media.pk not in ids:
                continue
            if issue and issue != "LINK_ONLY":
                issues.append(issue)
            if target is not None and target.pk == media.pk:
                ref = actual.pop((entry.pk, slot), None)
                if ref is None:
                    issues.append("REFERENCE_MISSING")
                elif (ref.owner_id, ref.value) != (entry.user_id, value):
                    issues.append("REFERENCE_MISMATCH")
    if actual:
        issues.append("REFERENCE_MISMATCH")
    return sorted(set(issues))
