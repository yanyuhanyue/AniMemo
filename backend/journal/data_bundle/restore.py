"""Explicit-request portable restore with durable receipts and atomic apply."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from journal.domain_services import JournalEntryService, JournalEntryServiceError
from journal.media_references import lock_media_owner
from journal.models import (
    BundleRestoreAdmission,
    BundleRestoreChunk,
    BundleRestoreSession,
    ExternalMediaIdentity,
    JournalEntry,
)
from journal.watch_history import WatchHistoryValidationError, replace_history

from . import restore_storage as storage
from .serializers import (
    BundleEntrySerializer,
    DataBundleSerializer,
    EntryDataSerializer,
)
from .services import DATA_BUNDLE_FORMAT, DATA_BUNDLE_SCHEMA_VERSION, _normalize_history
from .stream import BundleStreamError, iter_bundle_items

VALIDATOR_VERSION = "animemo-portable-restore-v1"
ACTIVE = ("validating", "committing")
OPEN = ("receiving", "validating", "ready", "committing")
TERMINAL = ("completed", "cancelled", "failed", "expired")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENCODER = DjangoJSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class BundleRestoreError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status_code = status


def _owner(user):
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise BundleRestoreError("owner_required", 403)
    if not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        raise BundleRestoreError("owner_required", 403)


def _session(user, session_id, *, lock=False):
    _owner(user)
    query = BundleRestoreSession.objects
    if lock:
        query = query.select_for_update()
    try:
        return query.get(pk=session_id, owner=user)
    except (BundleRestoreSession.DoesNotExist, ValueError) as error:
        raise BundleRestoreError("not_found", 404) from error


def _generation(session, value):
    if isinstance(value, bool) or not isinstance(value, int) or value != session.generation:
        raise BundleRestoreError("bundle_restore_stale", 409)


def _expired(session, now=None):
    now = now or timezone.now()
    return session.state in OPEN and (
        now >= session.created_at + timedelta(seconds=settings.BUNDLE_RESTORE_LIFETIME_SECONDS)
        or session.state not in ACTIVE and now >= session.touched_at + timedelta(seconds=settings.BUNDLE_RESTORE_IDLE_SECONDS)
    )


def _available(session, generation, states):
    _generation(session, generation)
    if _expired(session):
        raise BundleRestoreError("bundle_restore_expired", 410)
    if session.state not in states:
        raise BundleRestoreError("bundle_restore_state", 409)


def _admission():
    # Installed by the additive migration. Missing authority fails closed.
    try:
        return BundleRestoreAdmission.objects.select_for_update().get(pk=1)
    except BundleRestoreAdmission.DoesNotExist as error:
        raise BundleRestoreError("service_unavailable", 503) from error


def representation(session):
    return {
        "id": str(session.pk), "generation": session.generation, "state": session.state,
        "schema_version": session.schema_version, "expected_bytes": session.expected_bytes,
        "received_bytes": session.received_bytes, "sha256": session.sha256,
        "chunk_bytes": settings.BUNDLE_RESTORE_CHUNK_BYTES, "preview": session.preview,
        "receipt": session.receipt, "error_code": session.error_code,
        "cleanup_pending": session.cleanup_pending,
        "expires_at": (session.created_at + timedelta(seconds=settings.BUNDLE_RESTORE_LIFETIME_SECONDS)).isoformat(),
    }


def get_restore(*, user, session_id):
    return representation(_session(user, session_id))


def current_restore(*, user):
    _owner(user)
    session = BundleRestoreSession.objects.filter(owner=user, state__in=OPEN).first()
    return {"session": representation(session) if session else None}


def create_restore(*, user, payload):
    _owner(user)
    if not isinstance(payload, dict) or set(payload) != {"idempotency_key", "expected_bytes", "sha256", "schema_version"}:
        raise BundleRestoreError("invalid_request")
    expected = payload["expected_bytes"]
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise BundleRestoreError("invalid_request")
    if payload["schema_version"] != DATA_BUNDLE_SCHEMA_VERSION or isinstance(payload["schema_version"], bool):
        raise BundleRestoreError("unsupported_import_schema")
    digest = payload["sha256"]
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise BundleRestoreError("invalid_request")
    try:
        key = uuid.UUID(str(payload["idempotency_key"]))
    except (ValueError, TypeError) as error:
        raise BundleRestoreError("invalid_request") from error
    with transaction.atomic():
        lock_media_owner(user.pk)
        previous = BundleRestoreSession.objects.select_for_update().filter(owner=user, idempotency_key=key).first()
        if previous:
            if (previous.expected_bytes, previous.sha256, previous.schema_version) != (expected, digest, payload["schema_version"]):
                raise BundleRestoreError("bundle_restore_idempotency_conflict", 409)
            return representation(previous)
        if expected > settings.BUNDLE_RESTORE_RAW_BYTES:
            raise BundleRestoreError("bundle_restore_capacity", 413)
        if BundleRestoreSession.objects.filter(owner=user, state__in=OPEN).exists():
            raise BundleRestoreError("bundle_restore_active", 409)
        _admission()
        reserved = expected + settings.BUNDLE_RESTORE_NORMALIZED_BYTES + settings.BUNDLE_RESTORE_INDEX_BYTES
        used = BundleRestoreSession.objects.aggregate(total=Sum("reserved_bytes"))["total"] or 0
        if used + reserved > settings.BUNDLE_RESTORE_DISK_BYTES:
            raise BundleRestoreError("storage_exhausted", 507)
        root = settings.BUNDLE_RESTORE_ROOT
        existing = next((path for path in [root, *root.parents] if path.exists()), None)
        if existing is None or shutil.disk_usage(existing).free < used + reserved:
            raise BundleRestoreError("storage_exhausted", 507)
        session = BundleRestoreSession.objects.create(
            owner=user, idempotency_key=key, expected_bytes=expected, sha256=digest,
            normalized_limit=settings.BUNDLE_RESTORE_NORMALIZED_BYTES,
            single_limit=settings.BUNDLE_RESTORE_SINGLE_BYTES, index_limit=settings.BUNDLE_RESTORE_INDEX_BYTES,
            reserved_bytes=reserved,
        )
        return representation(session)


def _finish_failure(user, session_id, generation, code):
    with transaction.atomic():
        lock_media_owner(user.pk)
        session = _session(user, session_id, lock=True)
        if session.generation != generation or session.state not in OPEN:
            return
        session.state = "failed"
        session.error_code = code
        session.finished_at = timezone.now()
        session.operation_expires_at = None
        session.cleanup_pending = True
        try:
            session.physical_bytes = storage.physical_size(session)
        except (OSError, storage.RestoreStorageError):
            # Do not release the reservation when actual occupation is unknown.
            pass
        session.save()


def upload_chunk(*, user, session_id, generation, offset, data):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not isinstance(data, bytes):
        raise BundleRestoreError("invalid_request")
    if not 1 <= len(data) <= settings.BUNDLE_RESTORE_CHUNK_BYTES:
        raise BundleRestoreError("payload_too_large", 413)
    try:
        with transaction.atomic():
            lock_media_owner(user.pk)
            session = _session(user, session_id, lock=True)
            _available(session, generation, ("receiving",))
            expected_size = min(settings.BUNDLE_RESTORE_CHUNK_BYTES, session.expected_bytes - offset)
            if offset % settings.BUNDLE_RESTORE_CHUNK_BYTES or len(data) != expected_size:
                raise BundleRestoreError("bundle_restore_chunk_conflict", 409)
            digest = hashlib.sha256(data).hexdigest()
            receipt = session.chunks.filter(offset=offset).first()
            directory = storage.session_directory(session.pk, create=True)
            if receipt:
                if (receipt.size, receipt.sha256) != (len(data), digest):
                    raise BundleRestoreError("bundle_restore_chunk_conflict", 409)
                if storage.file_digest(storage.chunk_path(directory, offset)) != (receipt.size, receipt.sha256):
                    raise storage.RestoreStorageError("chunk_receipt_mismatch")
                return representation(session)
            if offset != session.received_bytes:
                raise BundleRestoreError("bundle_restore_chunk_conflict", 409)
            storage.persist_chunk(directory, offset, data)
            BundleRestoreChunk.objects.create(session=session, offset=offset, size=len(data), sha256=digest)
            session.received_bytes += len(data)
            session.physical_bytes = storage.physical_size(session)
            session.touched_at = timezone.now()
            session.save(update_fields=["received_bytes", "physical_bytes", "touched_at"])
            return representation(session)
    except (OSError, storage.RestoreStorageError) as error:
        _finish_failure(user, session_id, generation, "bundle_restore_storage")
        raise BundleRestoreError("bundle_restore_storage", 507) from error


def _claim(user, session_id, generation, state):
    with transaction.atomic():
        lock_media_owner(user.pk)
        session = _session(user, session_id, lock=True)
        if state == "committing" and session.state == "completed":
            return session
        allowed = ("receiving", "ready") if state == "validating" else ("ready",)
        _available(session, generation, allowed)
        if session.received_bytes != session.expected_bytes:
            raise BundleRestoreError("bundle_restore_incomplete", 409)
        _admission()
        if BundleRestoreSession.objects.filter(state__in=ACTIVE).count() >= 2:
            raise BundleRestoreError("bundle_restore_busy", 429)
        session.state = state
        session.operation_expires_at = timezone.now() + timedelta(seconds=settings.BUNDLE_RESTORE_OPERATION_SECONDS)
        session.save(update_fields=["state", "operation_expires_at"])
        return session


def _deadline(session):
    remaining = (session.operation_expires_at - timezone.now()).total_seconds()
    return time.monotonic() + remaining


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise BundleRestoreError("request_timeout", 408)


def _validated_item(item):
    serializer = BundleEntrySerializer(data=item)
    if not serializer.is_valid():
        raise BundleRestoreError("invalid_data_bundle")
    value = serializer.validated_data
    value["watch_history"] = _normalize_history(value["watch_history"])
    return value


def _validate_to_file(session):
    if session.index_limit < 8192:
        raise BundleRestoreError("bundle_restore_capacity", 413)
    directory = storage.discard_normalized(session)
    index_path = directory / "identities.sqlite3"
    with storage.open_private(index_path, create=True):
        pass
    deadline = _deadline(session)
    digest = hashlib.sha256()
    total_bytes = count = 0
    preview = []
    with closing(sqlite3.connect(index_path)) as index, closing(storage.ChunkReader(session)) as raw:
        index.execute("PRAGMA page_size=4096")
        index.execute("PRAGMA journal_mode=OFF")
        index.execute("PRAGMA cache_size=-1024")
        index.execute(f"PRAGMA max_page_count={max(2, session.index_limit // 4096)}")
        index.execute("CREATE TABLE identities(provider TEXT, external_id TEXT, PRIMARY KEY(provider, external_id)) WITHOUT ROWID")
        stream = iter_bundle_items(raw, max_item_bytes=session.single_limit,
                                   max_header_bytes=settings.BUNDLE_RESTORE_HEADER_BYTES,
                                   max_depth=settings.BUNDLE_RESTORE_MAX_DEPTH)
        with storage.open_private(directory / "normalizing.jsonl", create=True) as target:
            for item in stream:
                _check_deadline(deadline)
                value = _validated_item(item)
                for identity in value["external_identities"]:
                    try:
                        index.execute("INSERT INTO identities VALUES (?,?)", (identity["provider"], identity["external_id"]))
                    except sqlite3.IntegrityError as error:
                        raise BundleRestoreError("invalid_data_bundle") from error
                index.commit()
                if index_path.stat().st_size > session.index_limit:
                    raise BundleRestoreError("bundle_restore_capacity", 413)
                item_bytes = 0
                for part in ENCODER.iterencode(value):
                    data = part.encode("utf-8")
                    item_bytes += len(data)
                    total_bytes += len(data)
                    if item_bytes > session.single_limit or total_bytes + 1 > session.normalized_limit:
                        raise BundleRestoreError("bundle_restore_capacity", 413)
                    target.write(data)
                    digest.update(data)
                target.write(b"\n")
                digest.update(b"\n")
                total_bytes += 1
                count += 1
                if len(preview) < 50:
                    preview.append({"row": count, "title": value["entry"]["title"], "status": "ready", "reason": "等待恢复"})
            header = stream.header
            if header.get("format") != DATA_BUNDLE_FORMAT or header.get("schema_version") != DATA_BUNDLE_SCHEMA_VERSION:
                raise BundleRestoreError("unsupported_import_schema")
            validator = DataBundleSerializer(data={**header, "entries": []})
            if not validator.is_valid() or isinstance(header["schema_version"], bool):
                raise BundleRestoreError("invalid_data_bundle")
            _check_deadline(deadline)
            target.flush()
            os.fsync(target.fileno())
    _check_deadline(deadline)
    os.rename(directory / "normalizing.jsonl", directory / "normalized.jsonl")
    session.normalized_bytes = total_bytes
    session.normalized_sha256 = digest.hexdigest()
    session.validator_version = VALIDATOR_VERSION
    empty = not JournalEntry.objects.filter(user_id=session.owner_id, deleted_at__isnull=True).exists()
    session.preview = {"format": DATA_BUNDLE_FORMAT, "schema_version": 1, "total": count,
                       "ready": count if empty else 0, "items": preview, "items_truncated": count > len(preview),
                       "skipped_duplicates": 0, "errors": [] if empty else [{"code": "bundle_import_requires_empty_journal"}]}
    session.physical_bytes = storage.physical_size(session)


def validate_restore(*, user, session_id, generation):
    _claim(user, session_id, generation, "validating")
    try:
        # No owner or global admission lock is held during parsing. Keeping the
        # session row lock prevents cleanup from deleting active working files.
        with transaction.atomic():
            session = _session(user, session_id, lock=True)
            _available(session, generation, ("validating",))
            _validate_to_file(session)
            _check_deadline(_deadline(session))
            session.state = "ready"
            session.operation_expires_at = None
            session.touched_at = timezone.now()
            session.save()
            return representation(session)
    except BundleRestoreError as error:
        _finish_failure(user, session_id, generation, error.code)
        raise
    except DatabaseError as error:
        _finish_failure(user, session_id, generation, "import_commit_failed")
        raise BundleRestoreError("import_commit_failed", 503) from error
    except BundleStreamError as error:
        capacity = error.code in {"bundle_item_too_large", "bundle_header_too_large", "bundle_depth_exceeded"}
        code = "bundle_restore_capacity" if capacity else "invalid_data_bundle"
        _finish_failure(user, session_id, generation, code)
        raise BundleRestoreError(code, 413 if capacity else 400) from error
    except (WatchHistoryValidationError, ValueError, RecursionError, OSError, sqlite3.Error) as error:
        code = "bundle_restore_storage" if isinstance(error, (OSError, storage.RestoreStorageError, sqlite3.OperationalError)) else "invalid_data_bundle"
        _finish_failure(user, session_id, generation, code)
        raise BundleRestoreError(code, 507 if code == "bundle_restore_storage" else 400) from error


def _normalized_items(session, deadline):
    path = storage.session_directory(session.pk) / "normalized.jsonl"
    if storage.file_digest(path) != (session.normalized_bytes, session.normalized_sha256):
        raise BundleRestoreError("bundle_restore_storage", 507)
    digest = hashlib.sha256()
    with storage.open_private(path) as source:
        while True:
            _check_deadline(deadline)
            line = source.readline(session.single_limit + 2)
            if not line:
                break
            if len(line) > session.single_limit + 1 or not line.endswith(b"\n"):
                raise BundleRestoreError("bundle_restore_storage", 507)
            digest.update(line)
            yield _validated_item(json.loads(line))
    if digest.hexdigest() != session.normalized_sha256:
        raise BundleRestoreError("bundle_restore_storage", 507)


def commit_restore(*, user, session_id, generation):
    claimed = _claim(user, session_id, generation, "committing")
    if claimed.state == "completed":
        return representation(claimed)
    try:
        with transaction.atomic():
            lock_media_owner(user.pk)
            session = _session(user, session_id, lock=True)
            _available(session, generation, ("committing",))
            if session.validator_version != VALIDATOR_VERSION or session.schema_version != DATA_BUNDLE_SCHEMA_VERSION:
                raise BundleRestoreError("bundle_restore_stale", 409)
            if JournalEntry.objects.filter(user=user, deleted_at__isnull=True).exists():
                raise BundleRestoreError("bundle_import_requires_empty_journal", 409)
            deadline = _deadline(session)
            # Bind commit to the original bytes as well as normalized output.
            with closing(storage.ChunkReader(session)) as raw:
                while raw.read(65536):
                    _check_deadline(deadline)
            service = JournalEntryService(user)
            count = 0
            for item in _normalized_items(session, deadline):
                dto = service.create_from_fields(item["entry"], serializer_class=EntryDataSerializer,
                                                 source="bundle", allowed_fields=set(item["entry"]))
                entry = JournalEntry.objects.get(pk=dto["entry_id"], user=user)
                ExternalMediaIdentity.objects.bulk_create([
                    ExternalMediaIdentity(entry=entry, **identity) for identity in item["external_identities"]
                ])
                replace_history(user=user, entry=entry, records=item["watch_history"])
                count += 1
            _check_deadline(deadline)
            if count != session.preview["total"]:
                raise BundleRestoreError("bundle_restore_storage", 507)
            session.state = "completed"
            session.receipt = {"format": DATA_BUNDLE_FORMAT, "schema_version": 1, "created": count,
                               "total": count, "skipped_duplicates": 0, "errors": []}
            session.finished_at = timezone.now()
            session.operation_expires_at = None
            session.cleanup_pending = True
            session.save()
            # Keep bytes until bounded maintenance confirms their removal. This
            # also preserves outer-transaction rollback and lost-response retry.
            return representation(session)
    except BundleRestoreError as error:
        _finish_failure(user, session_id, generation, error.code)
        raise
    except DatabaseError as error:
        # The business transaction has rolled back before the terminal state is
        # recorded. Database failures must not leave a claimed active slot.
        _finish_failure(user, session_id, generation, "import_commit_failed")
        raise BundleRestoreError("import_commit_failed", 503) from error
    except (JournalEntryServiceError, WatchHistoryValidationError, OSError, storage.RestoreStorageError, ValueError) as error:
        code = error.code if isinstance(error, JournalEntryServiceError) else "import_commit_failed"
        _finish_failure(user, session_id, generation, code)
        raise BundleRestoreError(code, getattr(error, "status_code", 400)) from error


def cancel_restore(*, user, session_id, generation):
    with transaction.atomic():
        lock_media_owner(user.pk)
        session = _session(user, session_id, lock=True)
        if session.state in TERMINAL:
            return representation(session)
        _generation(session, generation)
        # Row acquisition waits for an active operation to reach a known state;
        # a completed commit is never reversed by a late cancel.
        session.state = "cancelled"
        session.generation += 1
        session.finished_at = timezone.now()
        session.operation_expires_at = None
        session.cleanup_pending = True
        session.save()
        return representation(session)


def cleanup_restores(*, limit=25, max_files=100):
    if not 1 <= limit <= 100 or not 1 <= max_files <= 1000:
        raise BundleRestoreError("invalid_limit")
    result = {"inspected": 0, "cleaned": 0, "expired": 0, "retired": 0, "deferred": 0}
    now = timezone.now()
    candidates = list(BundleRestoreSession.objects.filter(
        Q(cleanup_pending=True)
        | Q(state__in=TERMINAL, reserved_bytes__gt=0)
        | Q(state__in=TERMINAL, finished_at__lte=now - timedelta(seconds=settings.BUNDLE_RESTORE_RECEIPT_SECONDS))
        | Q(state__in=ACTIVE, operation_expires_at__lte=now)
        | Q(state__in=OPEN, created_at__lte=now - timedelta(seconds=settings.BUNDLE_RESTORE_LIFETIME_SECONDS))
        | Q(state__in=("receiving", "ready"), touched_at__lte=now - timedelta(seconds=settings.BUNDLE_RESTORE_IDLE_SECONDS))
        | Q(owner__isnull=True, state__in=OPEN)
    ).order_by("touched_at", "id").values_list("pk", "owner_id")[:limit])
    for session_id, owner_id in candidates:
        try:
            with transaction.atomic():
                if owner_id is not None:
                    owners = get_user_model().objects.select_for_update(skip_locked=True).filter(pk=owner_id)
                    if not owners.exists():
                        result["deferred"] += 1
                        continue
                session = BundleRestoreSession.objects.select_for_update(skip_locked=True).filter(pk=session_id).first()
                if session is None:
                    result["deferred"] += 1
                    continue
                result["inspected"] += 1
                if session.state in ACTIVE and session.operation_expires_at and now < session.operation_expires_at:
                    continue
                if session.state in ACTIVE or _expired(session, now) or session.owner_id is None and session.state in OPEN:
                    session.state = "expired"
                    session.generation += 1
                    session.finished_at = now
                    session.operation_expires_at = None
                    session.cleanup_pending = True
                    result["expired"] += 1
                if session.state in TERMINAL:
                    complete = storage.cleanup_files(session, max_files=max_files)
                    session.physical_bytes = storage.physical_size(session)
                    session.cleanup_pending = not complete
                    if complete:
                        session.reserved_bytes = 0
                        result["cleaned"] += 1
                    if complete and session.finished_at and now >= session.finished_at + timedelta(seconds=settings.BUNDLE_RESTORE_RECEIPT_SECONDS):
                        session.delete()
                        result["retired"] += 1
                        continue
                session.save()
        except (OSError, storage.RestoreStorageError, DatabaseError):
            result["deferred"] += 1
    return result
