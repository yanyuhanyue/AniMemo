from accounts.models import StaffProfile, UserSecurityProfile
from config.api_errors import public_failure
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from plugin_host.sdk import ColumnHookContext, run_hook
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Column, JournalEntry, UserSettings
from .serializers import UserSettingsSerializer
from .staff_common import _require_sensitive_reauthentication
from .staff_services import (
    StaffCapabilityPermission,
    assert_can_manage_user,
    ensure_not_last_active_superuser,
    record_audit,
    resolve_staff_role,
    revoke_user_sessions,
    staff_capabilities,
)
from .view_helpers import build_staff_user_data

User = get_user_model()


class StaffDashboardView(APIView):
    """Small, purpose-built read model for the custom staff control room."""

    permission_classes = [StaffCapabilityPermission]
    required_capability = "view_dashboard"

    def get(self, request):
        visible_columns = Column.objects.filter(deleted_at__isnull=True)
        columns = visible_columns.select_related("author").annotate(entry_count=Count("entries"))
        pending = columns.filter(status__in=[Column.Status.PENDING, Column.Status.REMOVAL_REQUESTED])[:12]
        recent_columns = columns[:8]
        entries = JournalEntry.objects.filter(deleted_at__isnull=True).select_related("user").order_by("-updated_at")[:12]
        journal_requests = (
            UserSettings.objects.select_related("user")
            .filter(public_status__in=[UserSettings.PublicStatus.PENDING, UserSettings.PublicStatus.APPROVED])
            .annotate(entry_count=Count("user__journal_entries", distinct=True))
            .order_by("public_status", "-updated_at")[:20]
        )
        recent_users = list(
            User.objects.annotate(
                entry_count=Count("journal_entries", distinct=True),
                column_count=Count("columns", distinct=True),
            ).order_by("-date_joined")[:100]
        )
        user_ids = [user.id for user in recent_users]
        settings_by_user = {
            item.user_id: item
            for item in UserSettings.objects.filter(user_id__in=user_ids)
        }
        security_profiles_by_user = {
            profile.user_id: profile
            for profile in UserSecurityProfile.objects.filter(user_id__in=user_ids)
        }
        staff_profiles_by_user = {
            profile.user_id: profile
            for profile in StaffProfile.objects.filter(user_id__in=user_ids)
        }
        actor_staff_profile = (
            None
            if request.user.is_superuser
            else StaffProfile.objects.filter(user_id=request.user.id).first()
        )
        actor_role = resolve_staff_role(request.user, staff_profile=actor_staff_profile)
        actor_capabilities = staff_capabilities(request.user, staff_profile=actor_staff_profile)

        def column_data(column):
            return {
                "id": column.id,
                "title": column.title,
                "summary": column.summary,
                "status": column.status,
                "status_display": column.get_status_display(),
                "featured": column.featured,
                "author": column.author.get_username(),
                "author_email": column.author.email,
                "entry_count": column.entry_count,
                "updated_at": column.updated_at,
                "published_at": column.published_at,
            }

        def entry_data(entry):
            return {
                "id": entry.id,
                "title": entry.title,
                "user": entry.user.get_username(),
                "email": entry.user.email,
                "status": entry.get_watch_status_display(),
                "score": entry.personal_score,
                "visibility": entry.get_visibility_display(),
                "updated_at": entry.updated_at,
            }

        def journal_data(settings_obj):
            return {
                "id": settings_obj.id,
                "nickname": settings_obj.nickname or settings_obj.user.get_username(),
                "username": settings_obj.user.get_username(),
                "email": settings_obj.user.email,
                "public_status": settings_obj.public_status,
                "public_status_display": settings_obj.get_public_status_display(),
                "is_public": settings_obj.allow_sharing,
                "public_slug": settings_obj.public_slug,
                "entry_count": settings_obj.entry_count,
                "updated_at": settings_obj.updated_at,
            }

        users = [
            build_staff_user_data(
                user,
                settings_by_user.get(user.id),
                request.user,
                security_profile=security_profiles_by_user.get(user.id),
                staff_profile=staff_profiles_by_user.get(user.id),
                entry_count=user.entry_count,
                column_count=user.column_count,
                actor_capabilities=actor_capabilities,
            )
            for user in recent_users
        ]

        return Response({
            "stats": {
                "users": User.objects.count(),
                "active_users": User.objects.filter(is_active=True).count(),
                "entries": JournalEntry.objects.filter(deleted_at__isnull=True).count(),
                "columns": Column.objects.filter(deleted_at__isnull=True).count(),
                "pending_columns": visible_columns.filter(status=Column.Status.PENDING).count(),
                "published_columns": visible_columns.filter(status=Column.Status.APPROVED, featured=True).count(),
                "removal_requests": visible_columns.filter(status=Column.Status.REMOVAL_REQUESTED).count(),
                "pending_journals": UserSettings.objects.filter(public_status=UserSettings.PublicStatus.PENDING).count(),
            },
            "pending_columns": [column_data(item) for item in pending],
            "recent_columns": [column_data(item) for item in recent_columns],
            "recent_entries": [entry_data(item) for item in entries],
            "journal_requests": [journal_data(item) for item in journal_requests],
            "users": users,
            "viewer": {
                "id": request.user.id,
                "is_superuser": request.user.is_superuser,
                "role": actor_role,
                "capabilities": actor_capabilities,
            },
        })


class StaffColumnReviewView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "moderate_content"

    def patch(self, request, pk):
        column = get_object_or_404(Column, pk=pk, deleted_at__isnull=True)
        next_status = request.data.get("status")
        featured = request.data.get("featured")
        reason = str(request.data.get("reason", "")).strip()[:500]
        before = {"status": column.status, "featured": column.featured, "moderation_reason": column.moderation_reason}
        allowed = {choice for choice, _label in Column.Status.choices}
        if next_status is not None:
            if next_status not in allowed:
                return Response({"detail": "不支持的专栏状态。"}, status=status.HTTP_400_BAD_REQUEST)
            if next_status == Column.Status.REJECTED and not reason:
                return Response({"detail": "驳回专栏时必须填写原因。"}, status=status.HTTP_400_BAD_REQUEST)
            column.status = next_status
            column.published_at = timezone.now() if next_status == Column.Status.APPROVED else None
            column.moderation_reason = reason
            column.moderated_by = request.user
            column.moderated_at = timezone.now()
        if featured is not None:
            column.featured = bool(featured)
        column.save(update_fields=["status", "featured", "published_at", "moderation_reason", "moderated_by", "moderated_at", "updated_at"])
        if before["status"] != Column.Status.APPROVED and next_status == Column.Status.APPROVED:
            run_hook("column.after_publish", ColumnHookContext(column_id=column.pk, actor_id=request.user.pk, source="staff-review"))
        record_audit(request, action="column.review", target=column, before=before, after={"status": column.status, "featured": column.featured, "moderation_reason": column.moderation_reason})
        return Response({
            "id": column.id,
            "status": column.status,
            "status_display": column.get_status_display(),
            "featured": column.featured,
            "published_at": column.published_at,
        })


class StaffPublicJournalReviewView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "moderate_content"

    def patch(self, request, pk):
        settings_obj = get_object_or_404(UserSettings.objects.select_related("user"), pk=pk)
        next_status = request.data.get("status")
        reason = str(request.data.get("reason", "")).strip()[:500]
        if next_status not in {
            UserSettings.PublicStatus.PRIVATE,
            UserSettings.PublicStatus.APPROVED,
        }:
            return Response({"detail": "不支持的公开手账状态。"}, status=status.HTTP_400_BAD_REQUEST)
        if next_status == UserSettings.PublicStatus.PRIVATE and settings_obj.public_status == UserSettings.PublicStatus.PENDING and not reason:
            return Response({"detail": "驳回公开申请时必须填写原因。"}, status=status.HTTP_400_BAD_REQUEST)
        before = {"public_status": settings_obj.public_status, "allow_sharing": settings_obj.allow_sharing, "review_reason": settings_obj.public_review_reason}
        settings_obj.public_status = next_status
        settings_obj.allow_sharing = next_status == UserSettings.PublicStatus.APPROVED
        settings_obj.public_review_reason = reason
        settings_obj.public_reviewed_by = request.user
        settings_obj.public_reviewed_at = timezone.now()
        settings_obj.save(update_fields=["public_status", "allow_sharing", "public_review_reason", "public_reviewed_by", "public_reviewed_at", "updated_at"])
        record_audit(request, action="journal.review", target=settings_obj, before=before, after={"public_status": settings_obj.public_status, "allow_sharing": settings_obj.allow_sharing, "review_reason": reason})
        return Response(UserSettingsSerializer(settings_obj, context={"request": request}).data)


class StaffUserPermissionsView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_users"
    throttle_scope = "two_factor"
    account_throttle_scope = "two_factor"
    throttle_account_fields = ("target",)

    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        try:
            assert_can_manage_user(request, user, action="permissions")
        except DjangoValidationError:
            return Response(
                public_failure(request=request, candidate_code="staff_user_access_denied", status_code=status.HTTP_403_FORBIDDEN),
                status=status.HTTP_403_FORBIDDEN,
            )
        before = {"is_active": user.is_active, "is_staff": user.is_staff, "is_superuser": user.is_superuser}
        requested = {key: request.data[key] for key in ("is_active", "is_staff", "is_superuser") if key in request.data}
        if not requested:
            return Response({"detail": "请提供需要修改的账号权限。"}, status=status.HTTP_400_BAD_REQUEST)
        if any(value not in {True, False} for value in requested.values()):
            return Response({"detail": "账号权限必须使用布尔值。"}, status=status.HTTP_400_BAD_REQUEST)
        if ("is_staff" in requested or "is_superuser" in requested) and not request.user.is_superuser:
            return Response({"detail": "只有超级管理员可以调整管理员权限。"}, status=status.HTTP_403_FORBIDDEN)
        if requested.get("is_superuser") is True:
            requested["is_staff"] = True
        if user.is_superuser and requested.get("is_superuser") is False:
            return Response({"detail": "不能通过此操作降级超级管理员，请使用受保护的角色流程。"}, status=status.HTTP_400_BAD_REQUEST)
        sensitive = user.is_staff or user.is_superuser or "is_staff" in requested or "is_superuser" in requested
        try:
            if sensitive:
                _require_sensitive_reauthentication(
                    request,
                    user,
                    "permissions",
                    force=("is_staff" in requested or "is_superuser" in requested),
                )
            with transaction.atomic():
                ensure_not_last_active_superuser(user, requested)
                user = User.objects.select_for_update().get(pk=user.pk)
                update_fields = []
                for field, value in requested.items():
                    setattr(user, field, value)
                    update_fields.append(field)
                user.save(update_fields=update_fields)
                if requested.get("is_staff") is False:
                    StaffProfile.objects.filter(user_id=user.pk).delete()
                revoke_user_sessions(user)
        except DjangoValidationError:
            return Response(
                public_failure(request=request, candidate_code="staff_permission_change_denied", status_code=status.HTTP_403_FORBIDDEN),
                status=status.HTTP_403_FORBIDDEN,
            )

        settings_obj = UserSettings.objects.filter(user=user).first()
        if requested.get("is_staff") is True and settings_obj and settings_obj.public_status == UserSettings.PublicStatus.PENDING:
            settings_obj.public_status = UserSettings.PublicStatus.APPROVED
            settings_obj.allow_sharing = True
            settings_obj.save(update_fields=["public_status", "allow_sharing", "updated_at"])

        record_audit(request, action="user.permissions", target=user, before=before, after={"is_active": user.is_active, "is_staff": user.is_staff, "is_superuser": user.is_superuser})
        return Response(build_staff_user_data(user, settings_obj, request.user))
