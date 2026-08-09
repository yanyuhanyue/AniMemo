from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from plugin_host.permissions import enabled_user_plugin


@dataclass(frozen=True)
class HostCapabilityError(ValueError):
    code: str
    detail: object
    status_code: int = 400

    def __str__(self):
        return str(self.detail)


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
    writable_fields = {
        "title",
        "japanese_title",
        "airing_period",
        "studio",
        "episodes",
        "description",
        "poster_url",
        "baike_url",
        "tags",
        "tag_colors",
        "personal_score",
        "watch_status",
        "review",
        "visibility",
    }

    def get_entry(self, entry_id):
        return _entry_dto(self._owned_entry(entry_id))

    def list_entries(self, *, query="", limit=100):
        from journal.models import JournalEntry

        user = self.user()
        limit = max(1, min(int(limit), 500))
        rows = JournalEntry.objects.filter(user=user, deleted_at__isnull=True).order_by("pk")
        normalized_query = str(query or "").strip()[:120]
        if normalized_query:
            rows = rows.filter(
                Q(title__icontains=normalized_query)
                | Q(japanese_title__icontains=normalized_query)
            )
        return [_entry_dto(entry) for entry in rows[:limit]]

    def create_entry(self, fields):
        from journal.serializers_entries import JournalEntrySerializer

        user = self.user()
        serializer = JournalEntrySerializer(data=self._fields(fields))
        self._validate(serializer)
        with transaction.atomic():
            entry = serializer.save(user=user)
        return _entry_dto(entry)

    def update_entry(self, entry_id, fields):
        from journal.models import JournalEntry
        from journal.serializers_entries import JournalEntrySerializer

        user = self.user()
        with transaction.atomic():
            try:
                entry = JournalEntry.objects.select_for_update().get(
                    pk=entry_id,
                    user=user,
                    deleted_at__isnull=True,
                )
            except (TypeError, ValueError, JournalEntry.DoesNotExist) as error:
                raise HostCapabilityError("entry_not_found", "番剧条目不存在。", 404) from error
            serializer = JournalEntrySerializer(entry, data=self._fields(fields), partial=True)
            self._validate(serializer)
            entry = serializer.save()
        return _entry_dto(entry)

    def _owned_entry(self, entry_id):
        from journal.models import JournalEntry

        user = self.user()
        try:
            return JournalEntry.objects.get(
                pk=entry_id,
                user=user,
                deleted_at__isnull=True,
            )
        except (TypeError, ValueError, JournalEntry.DoesNotExist) as error:
            raise HostCapabilityError("entry_not_found", "番剧条目不存在。", 404) from error

    def _fields(self, fields):
        if not isinstance(fields, dict) or set(fields) - self.writable_fields:
            raise HostCapabilityError("invalid_entry", "番剧条目 DTO 包含不允许的字段。")
        return dict(fields)

    @staticmethod
    def _validate(serializer):
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as error:
            raise HostCapabilityError("invalid_entry", error.detail) from error


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


def _entry_dto(entry):
    return {
        "entry_id": entry.pk,
        "title": entry.title,
        "japanese_title": entry.japanese_title,
        "airing_period": entry.airing_period,
        "studio": entry.studio,
        "episodes": entry.episodes,
        "description": entry.description,
        "poster_url": entry.poster_url,
        "baike_url": entry.baike_url,
        "tags": list(entry.tags or []),
        "tag_colors": dict(entry.tag_colors or {}),
        "personal_score": entry.personal_score,
        "watch_status": entry.watch_status,
        "review": entry.review,
        "visibility": entry.visibility,
    }


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
