from config.api_errors import public_failure
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from plugin_host.sdk import ColumnHookContext, run_hook
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from site_config.models import TagDefinition

from .models import Column, JournalEntry, UserSettings
from .staff_common import _tag_definition_data, _tag_definition_values
from .staff_services import StaffCapabilityPermission, record_audit


class StaffBulkActionView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "moderate_content"

    def post(self, request, kind):
        ids = list(dict.fromkeys(request.data.get("ids") or []))[:100]
        action = request.data.get("action")
        reason = str(request.data.get("reason", "")).strip()[:500]
        if not ids:
            return Response({"detail": "请至少选择一条记录。"}, status=status.HTTP_400_BAD_REQUEST)
        if action in {"reject", "recycle"} and not reason:
            return Response({"detail": "该操作必须填写原因。"}, status=status.HTTP_400_BAD_REQUEST)
        now = timezone.now()
        changed = 0
        if kind == "columns":
            items = list(Column.objects.filter(pk__in=ids))
            for item in items:
                before = {"status": item.status, "featured": item.featured, "deleted_at": item.deleted_at}
                if action == "approve":
                    item.status, item.published_at, item.moderation_reason = Column.Status.APPROVED, now, reason
                    item.moderated_by, item.moderated_at = request.user, now
                elif action == "reject":
                    item.status, item.published_at, item.featured, item.moderation_reason = Column.Status.REJECTED, None, False, reason
                    item.moderated_by, item.moderated_at = request.user, now
                elif action == "recycle":
                    item.deleted_at, item.deleted_by, item.deletion_reason, item.featured = now, request.user, reason, False
                elif action == "restore":
                    item.deleted_at, item.deleted_by, item.deletion_reason = None, None, ""
                else:
                    return Response({"detail": "不支持的批量操作。"}, status=status.HTTP_400_BAD_REQUEST)
                item.save()
                if action == "approve" and before["status"] != Column.Status.APPROVED:
                    run_hook("column.after_publish", ColumnHookContext(column_id=item.pk, actor_id=request.user.pk, source="staff-bulk"))
                changed += 1
                record_audit(request, action=f"column.{action}", target=item, before=before, after={"status": item.status, "deleted_at": item.deleted_at}, metadata={"reason": reason, "batch": True})
        elif kind == "entries":
            items = list(JournalEntry.objects.filter(pk__in=ids))
            if action not in {"recycle", "restore"}:
                return Response({"detail": "番剧记录仅支持移入回收站或恢复。"}, status=status.HTTP_400_BAD_REQUEST)
            for item in items:
                before = {"deleted_at": item.deleted_at}
                if action == "recycle":
                    item.deleted_at, item.deleted_by, item.deletion_reason = now, request.user, reason
                else:
                    item.deleted_at, item.deleted_by, item.deletion_reason = None, None, ""
                item.save(update_fields=["deleted_at", "deleted_by", "deletion_reason", "updated_at"])
                changed += 1
                record_audit(request, action=f"entry.{action}", target=item, before=before, after={"deleted_at": item.deleted_at}, metadata={"reason": reason, "batch": True})
        elif kind == "journals":
            items = list(UserSettings.objects.filter(pk__in=ids))
            if action not in {"approve", "reject"}:
                return Response({"detail": "公开手账仅支持通过或驳回。"}, status=status.HTTP_400_BAD_REQUEST)
            for item in items:
                before = {"public_status": item.public_status, "allow_sharing": item.allow_sharing}
                item.public_status = UserSettings.PublicStatus.APPROVED if action == "approve" else UserSettings.PublicStatus.PRIVATE
                item.allow_sharing = action == "approve"
                item.public_review_reason = reason
                item.public_reviewed_by = request.user
                item.public_reviewed_at = now
                item.save(update_fields=["public_status", "allow_sharing", "public_review_reason", "public_reviewed_by", "public_reviewed_at", "updated_at"])
                changed += 1
                record_audit(request, action=f"journal.{action}", target=item, before=before, after={"public_status": item.public_status}, metadata={"reason": reason, "batch": True})
        else:
            return Response({"detail": "不支持的批量资源。"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"detail": f"已处理 {changed} 条记录。", "changed": changed})


class StaffTagDefinitionListCreateView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def get(self, request):
        return Response({
            "results": [_tag_definition_data(item) for item in TagDefinition.objects.all()],
            "colors": [
                {"value": value, "label": label}
                for value, label in TagDefinition.Color.choices
            ],
        })

    def post(self, request):
        try:
            values = _tag_definition_values(request.data)
        except ValueError:
            return Response(
                public_failure(request=request, candidate_code="invalid_tag_definition", status_code=status.HTTP_400_BAD_REQUEST),
                status=status.HTTP_400_BAD_REQUEST,
            )
        if TagDefinition.objects.filter(name__iexact=values["name"]).exists():
            return Response({"detail": "同名标签已经存在。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            item = TagDefinition.objects.create(
                **values,
                created_by=request.user,
                updated_by=request.user,
            )
        except IntegrityError:
            return Response({"detail": "同名标签已经存在。"}, status=status.HTTP_400_BAD_REQUEST)
        after = _tag_definition_data(item)
        record_audit(request, action="tag.create", target=item, after=after)
        return Response(after, status=status.HTTP_201_CREATED)


class StaffTagDefinitionDetailView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def patch(self, request, pk):
        item = get_object_or_404(TagDefinition, pk=pk)
        before = _tag_definition_data(item)
        try:
            values = _tag_definition_values(request.data, partial=True)
        except ValueError:
            return Response(
                public_failure(request=request, candidate_code="invalid_tag_definition", status_code=status.HTTP_400_BAD_REQUEST),
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not values:
            return Response(before)
        if "name" in values and TagDefinition.objects.exclude(pk=item.pk).filter(name__iexact=values["name"]).exists():
            return Response({"detail": "同名标签已经存在。"}, status=status.HTTP_400_BAD_REQUEST)
        for field, value in values.items():
            setattr(item, field, value)
        item.updated_by = request.user
        try:
            item.save(update_fields=[*values.keys(), "updated_by", "updated_at"])
        except IntegrityError:
            return Response({"detail": "同名标签已经存在。"}, status=status.HTTP_400_BAD_REQUEST)
        after = _tag_definition_data(item)
        record_audit(request, action="tag.update", target=item, before=before, after=after)
        return Response(after)

    def delete(self, request, pk):
        item = get_object_or_404(TagDefinition, pk=pk)
        before = _tag_definition_data(item)
        target_id = str(item.pk)
        target_label = item.name
        item.delete()
        record_audit(
            request,
            action="tag.delete",
            target_type="TagDefinition",
            target_id=target_id,
            target_label=target_label,
            before=before,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
