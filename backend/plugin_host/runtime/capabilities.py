from __future__ import annotations

from plugin_host.permissions import enabled_user_plugin
from plugin_host.errors import HostCapabilityError

from journal.domain_services import JournalEntryService, JournalEntryServiceError


class _Capability:
    def __init__(self, plugin_slug, actor):
        self.plugin_slug = plugin_slug
        self.actor = actor

    def user(self):
        user = getattr(self.actor, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            raise HostCapabilityError("plugin_context_forbidden", "插件能力缺少已认证用户上下文。", 403)
        if enabled_user_plugin(self.plugin_slug, user) is None:
            raise HostCapabilityError("plugin_disabled", "当前用户未启用此插件。", 403)
        return user


class PluginJournalCapability:
    def __init__(self, plugin_slug):
        self.plugin_slug = plugin_slug

    def bind(self, actor):
        return BoundJournalCapability(self.plugin_slug, actor)


class BoundJournalCapability(_Capability):
    def get_entry(self, entry_id):
        try:
            return JournalEntryService(self.user()).get(entry_id)
        except JournalEntryServiceError as error:
            raise HostCapabilityError(error.code, error.detail, error.status_code) from error

    def list_entries(self, *, query="", limit=100):
        try:
            return JournalEntryService(self.user()).list(query=query, limit=limit)
        except JournalEntryServiceError as error:
            raise HostCapabilityError(error.code, error.detail, error.status_code) from error

    def create_entry(self, fields):
        from journal.serializers_entries import JournalEntrySerializer

        try:
            return JournalEntryService(self.user()).create_from_fields(
                self._fields(fields),
                serializer_class=JournalEntrySerializer,
                source="plugin",
            )
        except JournalEntryServiceError as error:
            raise HostCapabilityError(error.code, error.detail, error.status_code) from error

    def update_entry(self, entry_id, fields):
        from journal.serializers_entries import JournalEntrySerializer

        try:
            return JournalEntryService(self.user()).update_from_fields(
                entry_id,
                self._fields(fields),
                serializer_class=JournalEntrySerializer,
                source="plugin",
            )
        except JournalEntryServiceError as error:
            raise HostCapabilityError(error.code, error.detail, error.status_code) from error

    def _fields(self, fields):
        return JournalEntryService.validate_fields(fields)


class PluginWatchHistoryCapability:
    def __init__(self, plugin_slug):
        self.plugin_slug = plugin_slug

    def bind(self, actor):
        return BoundWatchHistoryCapability(self.plugin_slug, actor)


class PluginAnalyticsCapability:
    def __init__(self, plugin_slug):
        self.plugin_slug = plugin_slug

    def bind(self, actor):
        return BoundAnalyticsCapability(self.plugin_slug, actor)


class BoundAnalyticsCapability(_Capability):
    def get(self, *, start=None, end=None):
        from journal.analytics import AnalyticsRangeError, build_user_analytics

        user = self.user()
        try:
            return build_user_analytics(user=user, start=start, end=end)
        except AnalyticsRangeError as error:
            raise HostCapabilityError("invalid_analytics_range", str(error)) from error


class BoundWatchHistoryCapability(_Capability):
    def normalize(self, records):
        from journal.watch_history import WatchHistoryValidationError, normalize_watch_history_records

        self.user()
        try:
            return normalize_watch_history_records(records)
        except WatchHistoryValidationError as error:
            raise HostCapabilityError(error.code, error.detail) from error

    def list_history(self, entry_id):
        from journal.watch_history import list_history

        user, entry = self._owned_entry(entry_id)
        return [_history_dto(record) for record in list_history(user=user, entry=entry)]

    def add_history(self, entry_id, record):
        from journal.watch_history import WatchHistoryValidationError, add_history

        user, entry = self._owned_entry(entry_id)
        try:
            result, created = add_history(user=user, entry=entry, record=record)
        except WatchHistoryValidationError as error:
            raise HostCapabilityError(error.code, error.detail) from error
        return {"created": created, "record": _history_dto(result), "total": entry.watch_history_records.count()}

    def merge_history(self, entry_id, records):
        from journal.watch_history import WatchHistoryValidationError, merge_history

        user, entry = self._owned_entry(entry_id)
        try:
            results, created, skipped = merge_history(user=user, entry=entry, records=records)
        except WatchHistoryValidationError as error:
            raise HostCapabilityError(error.code, error.detail) from error
        return {
            "records": [_history_dto(record) for record in results],
            "created": created,
            "skipped": skipped,
            "total": entry.watch_history_records.count(),
        }

    def _owned_entry(self, entry_id):
        from journal.models import JournalEntry

        user = self.user()
        try:
            entry = JournalEntry.objects.get(
                pk=entry_id,
                user=user,
                deleted_at__isnull=True,
            )
        except (TypeError, ValueError, JournalEntry.DoesNotExist) as error:
            raise HostCapabilityError("entry_not_found", "番剧条目不存在。", 404) from error
        return user, entry


def _history_dto(record):
    watched_on = record.watched_on
    if hasattr(watched_on, "isoformat"):
        watched_on = watched_on.isoformat()
    return {
        "id": record.pk,
        "watched_on": str(watched_on),
        "watched_label": record.watched_label,
        "brush_number": record.brush_number,
        "brush_label": record.brush_label,
        "episode_start": record.episode_start,
        "episode_end": record.episode_end,
        "notes": list(record.notes or []),
        "metadata": dict(record.metadata or {}),
        "sequence": record.sequence,
    }
