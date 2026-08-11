from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from plugin_host.hooks import run_hook

from .models import JournalEntry


class JournalEntryServiceError(ValueError):
    def __init__(self, code, detail, status_code=400):
        super().__init__(str(detail))
        self.code = code
        self.detail = detail
        self.status_code = status_code


class JournalEntryService:
    """Owner-scoped JournalEntry DTO and mutation boundary shared by transports."""

    writable_fields = frozenset({
        "title", "japanese_title", "airing_period", "studio", "episodes",
        "description", "poster_url", "baike_url", "tags", "tag_colors",
        "personal_score", "watch_status", "review", "visibility",
    })

    def __init__(self, user):
        self.user = user

    @classmethod
    def validate_fields(cls, fields, *, allowed_fields=None):
        allowed = cls.writable_fields if allowed_fields is None else frozenset(allowed_fields)
        if not isinstance(fields, dict) or set(fields) - allowed:
            raise JournalEntryServiceError("invalid_entry", "番剧条目 DTO 包含不允许的字段。")
        return dict(fields)

    def list(self, *, query="", limit=100):
        self._require_user()
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError) as error:
            raise JournalEntryServiceError("invalid_limit", "条目数量限制无效。") from error
        rows = JournalEntry.objects.filter(
            user=self.user,
            deleted_at__isnull=True,
        ).order_by("pk")
        normalized_query = str(query or "").strip()[:120]
        if normalized_query:
            rows = rows.filter(
                Q(title__icontains=normalized_query)
                | Q(japanese_title__icontains=normalized_query)
            )
        return [self.to_dto(entry) for entry in rows[:limit]]

    def get(self, entry_id):
        return self.to_dto(self._owned_entry(entry_id))

    def create(self, serializer, *, source="core"):
        self._require_user()
        try:
            entry = serializer.save(user=self.user)
        except ValidationError as error:
            raise JournalEntryServiceError("invalid_entry", error.detail) from error
        from plugin_host.sdk.types import JournalHookContext

        run_hook(
            "journal.after_create",
            JournalHookContext(user_id=entry.user_id, journal_entry_id=entry.pk, source=source),
        )
        return self.to_dto(entry)

    def create_from_fields(
        self,
        fields,
        *,
        serializer_class,
        source="core",
        context=None,
        allowed_fields=None,
    ):
        self._require_user()
        serializer = serializer_class(
            data=self.validate_fields(fields, allowed_fields=allowed_fields),
            context=context or {},
        )
        self._validate(serializer)
        return self.create(serializer, source=source)

    def update(self, serializer, *, source="core"):
        self._require_user()
        entry = serializer.instance
        if entry.user_id != self.user.pk or entry.deleted_at is not None:
            raise JournalEntryServiceError("entry_not_found", "番剧条目不存在。", 404)
        try:
            entry = serializer.save()
        except ValidationError as error:
            raise JournalEntryServiceError("invalid_entry", error.detail) from error
        from plugin_host.sdk.types import JournalHookContext

        run_hook(
            "journal.after_update",
            JournalHookContext(user_id=entry.user_id, journal_entry_id=entry.pk, source=source),
        )
        return self.to_dto(entry)

    def update_from_fields(
        self,
        entry_id,
        fields,
        *,
        serializer_class,
        source="core",
        context=None,
        allowed_fields=None,
    ):
        self._require_user()
        with transaction.atomic():
            entry = self._owned_entry(entry_id, lock=True)
            serializer = serializer_class(
                entry,
                data=self.validate_fields(fields, allowed_fields=allowed_fields),
                partial=True,
                context=context or {},
            )
            self._validate(serializer)
            return self.update(serializer, source=source)

    def delete(self, entry_id, *, source="core"):
        """Permanently delete one owner-scoped entry and emit one mutation hook."""
        self._require_user()
        with transaction.atomic():
            entry = self._owned_entry(entry_id, lock=True)
            user_id = entry.user_id
            entry.delete()
        from plugin_host.sdk.types import JournalHookContext

        run_hook(
            "journal.after_delete",
            JournalHookContext(user_id=user_id, journal_entry_id=entry_id, source=source),
        )
        return {"entry_id": entry_id, "deleted": True}

    @classmethod
    def to_dto(cls, entry):
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

    def _require_user(self):
        if not self.user or not getattr(self.user, "is_authenticated", True):
            raise JournalEntryServiceError("owner_required", "番剧条目必须绑定已认证用户。", 403)

    def _owned_entry(self, entry_id, *, lock=False):
        queryset = JournalEntry.objects
        if lock:
            queryset = queryset.select_for_update()
        try:
            return queryset.get(pk=entry_id, user=self.user, deleted_at__isnull=True)
        except (TypeError, ValueError, JournalEntry.DoesNotExist) as error:
            raise JournalEntryServiceError("entry_not_found", "番剧条目不存在。", 404) from error

    @staticmethod
    def _validate(serializer):
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as error:
            raise JournalEntryServiceError("invalid_entry", error.detail) from error
