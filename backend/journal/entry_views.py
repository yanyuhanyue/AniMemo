from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from plugin_host.permissions import plugin_permissions_for_user
from plugin_host.sdk import ColumnHookContext, JournalHookContext, run_hook

from .models import Column, JournalEntry, QuickFilter, UserSettings
from .pagination import FlexiblePageNumberPagination
from .permissions import IsOwner
from .serializers import ColumnSerializer, JournalEntrySerializer, QuickFilterSerializer, UserSettingsSerializer
from .staff_services import staff_capabilities


class JournalEntryViewSet(viewsets.ModelViewSet):
    serializer_class = JournalEntrySerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "japanese_title", "studio", "review"]
    ordering_fields = ["updated_at", "created_at", "airing_period", "personal_score", "title"]
    ordering = ["-updated_at"]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        queryset = JournalEntry.objects.filter(user=self.request.user, deleted_at__isnull=True)
        status_value = self.request.query_params.get("status")
        visibility = self.request.query_params.get("visibility")
        tag = self.request.query_params.get("tag")
        year = self.request.query_params.get("year")
        if status_value:
            queryset = queryset.filter(watch_status=status_value)
        if visibility:
            queryset = queryset.filter(visibility=visibility)
        if tag:
            queryset = queryset.filter(tags__icontains=tag)
        if year:
            queryset = queryset.filter(airing_period__startswith=year)
        return queryset

    def perform_create(self, serializer):
        entry = serializer.save(user=self.request.user)
        run_hook("journal.after_create", JournalHookContext(user_id=entry.user_id, journal_entry_id=entry.pk, source="api"))

    def perform_update(self, serializer):
        entry = serializer.save()
        run_hook("journal.after_update", JournalHookContext(user_id=entry.user_id, journal_entry_id=entry.pk, source="api"))

    def perform_destroy(self, instance):
        entry_id, user_id = instance.pk, instance.user_id
        super().perform_destroy(instance)
        run_hook("journal.after_delete", JournalHookContext(user_id=user_id, journal_entry_id=entry_id, source="api"))


class QuickFilterViewSet(viewsets.ModelViewSet):
    serializer_class = QuickFilterSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]

    def get_queryset(self):
        return QuickFilter.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ColumnViewSet(viewsets.ModelViewSet):
    serializer_class = ColumnSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        return Column.objects.filter(author=self.request.user, deleted_at__isnull=True).prefetch_related("entries")

    def perform_create(self, serializer):
        column = serializer.save(author=self.request.user)

    def perform_update(self, serializer):
        previous_status = serializer.instance.status
        column = serializer.save()
        if previous_status != Column.Status.APPROVED and column.status == Column.Status.APPROVED:
            run_hook("column.after_publish", ColumnHookContext(column_id=column.pk, actor_id=self.request.user.pk, source="api"))

    def perform_destroy(self, instance):
        column_id = instance.pk
        actor_id = self.request.user.pk
        author_id = instance.author_id
        super().perform_destroy(instance)
        run_hook(
            "column.after_delete",
            ColumnHookContext(column_id=column_id, actor_id=actor_id, source="api", author_id=author_id),
        )

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        column = self.get_object()
        if column.status not in {Column.Status.DRAFT, Column.Status.REJECTED}:
            return Response({"detail": "当前状态不能重复投稿。"}, status=status.HTTP_400_BAD_REQUEST)
        column.status = Column.Status.PENDING
        column.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(column).data)

    @action(detail=True, methods=["post"])
    def request_removal(self, request, pk=None):
        column = self.get_object()
        if column.status != Column.Status.APPROVED:
            return Response({"detail": "只有已发布专栏可以申请下架。"}, status=status.HTTP_400_BAD_REQUEST)
        column.status = Column.Status.REMOVAL_REQUESTED
        column.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(column).data)


class MeView(APIView):
    def get(self, request):
        settings_obj, _ = UserSettings.objects.get_or_create(user=request.user, defaults={"nickname": request.user.username})
        data = UserSettingsSerializer(settings_obj, context={"request": request}).data
        data["entry_count"] = request.user.journal_entries.filter(deleted_at__isnull=True).count()
        data["capabilities"] = staff_capabilities(request.user)
        data["pluginPermissions"] = plugin_permissions_for_user(request.user)
        return Response(data)


class UserSettingsView(APIView):
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_object(self, request):
        return UserSettings.objects.get_or_create(user=request.user, defaults={"nickname": request.user.username})[0]

    def get(self, request):
        return Response(UserSettingsSerializer(self.get_object(request), context={"request": request}).data)

    def patch(self, request):
        serializer = UserSettingsSerializer(self.get_object(request), data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PublicJournalStatusView(APIView):
    def get_object(self, request):
        return UserSettings.objects.get_or_create(
            user=request.user,
            defaults={"nickname": request.user.username},
        )[0]

    def post(self, request):
        settings_obj = self.get_object(request)
        if request.user.is_staff or request.user.is_superuser:
            settings_obj.public_status = UserSettings.PublicStatus.APPROVED
            settings_obj.allow_sharing = True
            settings_obj.save(update_fields=["public_status", "allow_sharing", "updated_at"])
            return Response(UserSettingsSerializer(settings_obj, context={"request": request}).data)
        if settings_obj.public_status == UserSettings.PublicStatus.PENDING:
            return Response({"detail": "分享申请正在审核中。"}, status=status.HTTP_409_CONFLICT)
        if settings_obj.public_status == UserSettings.PublicStatus.APPROVED:
            return Response({"detail": "个人手账已经公开。"}, status=status.HTTP_409_CONFLICT)
        settings_obj.public_status = UserSettings.PublicStatus.PENDING
        settings_obj.allow_sharing = False
        settings_obj.public_review_reason = ""
        settings_obj.save(update_fields=["public_status", "allow_sharing", "public_review_reason", "updated_at"])
        return Response(UserSettingsSerializer(settings_obj, context={"request": request}).data, status=status.HTTP_202_ACCEPTED)

    def patch(self, request):
        settings_obj = self.get_object(request)
        settings_obj.public_status = UserSettings.PublicStatus.PRIVATE
        settings_obj.allow_sharing = False
        settings_obj.save(update_fields=["public_status", "allow_sharing", "updated_at"])
        return Response(UserSettingsSerializer(settings_obj, context={"request": request}).data)
