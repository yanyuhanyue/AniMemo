"""Invocation-local facts for a PostgreSQL read-only inventory snapshot.

This object is never an admission or cleanup authority. All domains are loaded
before lookups are permitted; absence is meaningful only in a complete domain.
"""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass

from django.conf import settings
from django.db import connection
from site_config.media_storage.identity import resolve_url_candidates
from site_config.models import (
    MediaObject,
    MediaStorageBackend,
    MediaWriteReservation,
    SiteSettings,
)

from .media_references import PosterUsage, _entry_slot_sizes
from .models import Column, JournalEntry, JournalMediaReference, UserSettings


class ReadContextIncomplete(RuntimeError):
    """A complete, unchanged read context cannot be established."""


@dataclass(frozen=True)
class _Reference:
    entry_id: int
    owner_id: int
    slot: str
    media_id: object
    value: str
    media: MediaObject


class InventoryReadContext:
    ENTRY_FIELDS = ("id", "user_id", "poster_file", "custom_poster_url", "deleted_at")
    CONFIG_FIELDS = ("ANIMEMO_MEDIA_PUBLIC_ORIGIN", "MEDIA_HISTORICAL_PUBLIC_BASES", "MEDIA_LOCAL_STORAGE_ROOT")

    def __init__(self):
        self._active = False
        self._complete = False
        if connection.vendor != "postgresql" or not connection.in_atomic_block:
            raise ReadContextIncomplete("Inventory context requires a new PostgreSQL read-only snapshot")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')")
            if cursor.fetchone() != ("repeatable read", "on"):
                raise ReadContextIncomplete("Inventory context requires REPEATABLE READ READ ONLY")
        self._config = deepcopy(self._configuration())
        self._active = True
        self._backends = list(MediaStorageBackend.objects.only(
            "id", "backend_type", "local_root", "local_public_base_url", "public_base_url").order_by("pk"))
        backends = {backend.pk: backend for backend in self._backends}
        self._backend_ids = frozenset(backends)
        self.objects = list(MediaObject.objects.only(
            "id", "storage_backend_id", "object_key", "size_bytes", "sha256", "upload_owner_id",
            "public_url_snapshot", "public_url_identity", "reference_inventory_complete", "lifecycle"
        ).order_by("pk"))
        self._media = {media.pk: media for media in self.objects}
        self._locations, self._digests = defaultdict(list), defaultdict(list)
        self._owners = {media.pk: set() for media in self.objects}
        names = {media.reference_name: media.pk for media in self.objects}
        for media in self.objects:
            if media.storage_backend_id not in backends:
                raise ReadContextIncomplete("Media backend domain is incomplete")
            media._state.fields_cache["storage_backend"] = backends[media.storage_backend_id]
            self._locations[(media.storage_backend_id, media.object_key)].append(media)
            self._digests[media.public_url_identity].append(media)
            if media.upload_owner_id is not None:
                self._owners[media.pk].add(media.upload_owner_id)
        self._entries = list(JournalEntry.objects.order_by("pk").values_list(*self.ENTRY_FIELDS))
        self._model_entry_fields = tuple(field.attname for field in JournalEntry._meta.concrete_fields
                                         if field.attname in self.ENTRY_FIELDS)
        self._model_entry_positions = tuple(self.ENTRY_FIELDS.index(name) for name in self._model_entry_fields)
        for _pk, owner_id, poster, _url, _deleted_at in self._entries:
            # Ownership requires the exact persisted string, as in the live
            # queryset. A UUID that merely parses is not equivalent evidence.
            if poster in names:
                self._owners[names[poster]].add(owner_id)
        self.references, self._entry_refs = [], defaultdict(dict)
        for entry_id, owner_id, slot, media_id, value in JournalMediaReference.objects.order_by("pk").values_list(
                "entry_id", "owner_id", "slot", "media_id", "value"):
            if media_id not in self._media:
                raise ReadContextIncomplete("Reference media domain is incomplete")
            ref = _Reference(entry_id, owner_id, slot, media_id, value, self._media[media_id])
            self.references.append(ref)
            self._entry_refs[entry_id][slot] = ref
            self._owners[media_id].add(owner_id)
        self.roles = []
        for model, field, owner_field in ((UserSettings, "avatar", "user_id"), (Column, "cover", "author_id"),
                                          (SiteSettings, "site_avatar", None)):
            columns = ("pk", field, owner_field) if owner_field else ("pk", field)
            for row in model.objects.exclude(**{field: ""}).exclude(**{field: None}).values_list(*columns):
                pk, value = row[:2]
                self.roles.append((model._meta.label, pk, field, value))
                if owner_field and value in names:
                    self._owners[names[value]].add(row[2])
        self.unresolved_writes = list(MediaWriteReservation.objects.filter(
            status__in=["pending", "cleanup_ready", "cleanup_failed"]).order_by("pk").values(
                "id", "status", "size_bytes", "cleanup_error"))
        self._url_cache = {}
        self._complete = True
        self.check()

    def _configuration(self):
        return tuple(getattr(settings, field, None) for field in self.CONFIG_FIELDS)

    def check(self):
        if not self._active or not self._complete or not connection.in_atomic_block:
            raise ReadContextIncomplete("Inventory context is incomplete or outside its invocation")
        if self._configuration() != self._config:
            raise ReadContextIncomplete("Inventory configuration changed during its invocation")

    def close(self):
        self._active = False

    def entries(self):
        self.check()
        for row in self._entries:
            # from_db consumes its values in concrete-field order, even when
            # the scalar query selected the columns in a different order.
            yield JournalEntry.from_db("default", self._model_entry_fields,
                                       tuple(row[index] for index in self._model_entry_positions))

    def media_for_id(self, media_id):
        self.check()
        return self._media.get(media_id)

    def owners_for(self, media):
        self.check()
        if media.pk not in self._owners:
            raise ReadContextIncomplete("Owner evidence domain is incomplete")
        return self._owners[media.pk]

    def backends(self):
        self.check()
        return self._backends

    def at_location(self, backend, key):
        self.check()
        if backend.pk not in self._backend_ids:
            raise ReadContextIncomplete("URL backend domain is incomplete")
        return self._locations.get((backend.pk, key), ())

    def with_snapshot_digest(self, digest):
        self.check()
        return self._digests.get(digest, ())

    def resolve_candidates(self, value):
        self.check()
        if value not in self._url_cache:
            self._url_cache[value] = resolve_url_candidates(value, source=self)
        return self._url_cache[value]

    def usage_rows(self):
        self.check()
        totals, unknown = defaultdict(int), defaultdict(list)
        for entry in self.entries():
            totals[entry.user_id] += 0
            for slot, size in _entry_slot_sizes(entry, self._entry_refs.get(entry.pk, {}), self._url_cache,
                                                read_source=self).items():
                if size is None:
                    unknown[entry.user_id].append((entry.pk, slot))
                else:
                    totals[entry.user_id] += size
        return [{"owner_id": owner_id, "status": (usage := PosterUsage(totals[owner_id], tuple(unknown[owner_id]))).status,
                 "known_bytes": usage.known_bytes, "unknown_slots": list(usage.unknown_slots)}
                for owner_id in sorted(totals)]
