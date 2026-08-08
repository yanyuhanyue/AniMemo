from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AdminAuditLog, Column, JournalEntry, UserSettings
from .staff_common import _column_data, _entry_data, _journal_data, _paginate, _user_data, _validation_detail
from .staff_services import StaffCapabilityPermission, assert_can_manage_user


User = get_user_model()


class StaffResourceListView(APIView):
    permission_classes = [StaffCapabilityPermission]

    def get_permissions(self):
        kind = self.kwargs.get("kind")
        self.required_capability = "manage_users" if kind == "users" else "view_audit" if kind == "audit" else "moderate_content"
        return super().get_permissions()

    def get(self, request, kind):
        query = request.query_params.get("q", "").strip()
        state = request.query_params.get("status", "").strip()
        if kind == "users":
            queryset = User.objects.select_related("journal_settings", "security_profile", "staff_profile").annotate(
                entry_count=Count("journal_entries", filter=Q(journal_entries__deleted_at__isnull=True), distinct=True),
                column_count=Count("columns", filter=Q(columns__deleted_at__isnull=True), distinct=True),
            ).order_by("-date_joined")
            if query:
                queryset = queryset.filter(Q(username__icontains=query) | Q(email__icontains=query) | Q(journal_settings__nickname__icontains=query))
            if state == "active":
                queryset = queryset.filter(is_active=True)
            elif state == "disabled":
                queryset = queryset.filter(is_active=False)
            elif state == "staff":
                queryset = queryset.filter(is_staff=True)
            return _paginate(request, queryset, lambda item: _user_data(item, request.user))

        if kind == "columns":
            queryset = Column.objects.filter(deleted_at__isnull=True).select_related("author").annotate(entry_count=Count("entries", distinct=True)).order_by("-updated_at")
            if query:
                queryset = queryset.filter(Q(title__icontains=query) | Q(summary__icontains=query) | Q(author__username__icontains=query) | Q(author__email__icontains=query))
            if state:
                queryset = queryset.filter(status=state)
            return _paginate(request, queryset, lambda item: _column_data(request, item))

        if kind == "journals":
            queryset = UserSettings.objects.select_related("user").annotate(entry_count=Count("user__journal_entries", filter=Q(user__journal_entries__deleted_at__isnull=True))).order_by("public_status", "-updated_at")
            if query:
                queryset = queryset.filter(Q(nickname__icontains=query) | Q(user__username__icontains=query) | Q(user__email__icontains=query))
            if state:
                queryset = queryset.filter(public_status=state)
            return _paginate(request, queryset, lambda item: _journal_data(request, item))

        if kind == "entries":
            queryset = JournalEntry.objects.filter(deleted_at__isnull=True).select_related("user").order_by("-updated_at")
            if query:
                queryset = queryset.filter(Q(title__icontains=query) | Q(japanese_title__icontains=query) | Q(user__username__icontains=query) | Q(user__email__icontains=query))
            if state:
                queryset = queryset.filter(watch_status=state)
            return _paginate(request, queryset, lambda item: _entry_data(request, item))

        if kind == "audit":
            queryset = AdminAuditLog.objects.select_related("actor").all()
            if query:
                queryset = queryset.filter(Q(action__icontains=query) | Q(target_label__icontains=query) | Q(actor__username__icontains=query))
            if state:
                queryset = queryset.filter(action=state)
            return _paginate(request, queryset, lambda item: {
                "id": item.id,
                "actor": item.actor.get_username() if item.actor else "system",
                "action": item.action,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "target_label": item.target_label,
                "before": item.before,
                "after": item.after,
                "metadata": item.metadata,
                "ip_address": item.ip_address,
                "user_agent": item.user_agent,
                "created_at": item.created_at,
            })

        if kind == "recycle":
            items = [
                {"resource_type": "column", **_column_data(request, item)}
                for item in Column.objects.filter(deleted_at__isnull=False).select_related("author")
            ] + [
                {"resource_type": "entry", **_entry_data(request, item)}
                for item in JournalEntry.objects.filter(deleted_at__isnull=False).select_related("user")
            ]
            items.sort(key=lambda item: item.get("deleted_at") or timezone.now(), reverse=True)
            if query:
                needle = query.lower()
                items = [item for item in items if needle in f"{item.get('title', '')} {item.get('author', '')} {item.get('user', '')}".lower()]
            return _paginate(request, items, lambda item: item)

        return Response({"detail": "不支持的后台资源。"}, status=status.HTTP_404_NOT_FOUND)


class StaffResourceDetailView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "moderate_content"

    def get(self, request, kind, pk):
        if kind == "columns":
            item = get_object_or_404(Column.objects.select_related("author").prefetch_related("entries__user"), pk=pk)
            return Response(_column_data(request, item, detail=True))
        if kind == "journals":
            item = get_object_or_404(UserSettings.objects.select_related("user"), pk=pk)
            return Response(_journal_data(request, item, detail=True))
        if kind == "entries":
            item = get_object_or_404(JournalEntry.objects.select_related("user"), pk=pk)
            return Response(_entry_data(request, item, detail=True))
        return Response({"detail": "不支持的详情资源。"}, status=status.HTTP_404_NOT_FOUND)

