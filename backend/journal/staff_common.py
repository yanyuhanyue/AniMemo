from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.paginator import Paginator
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response

from accounts.models import StaffProfile
from site_config.models import TagDefinition

from .auth_service import authenticate_with_second_factor
from .models import Column, JournalEntry, UserSettings
from .staff_services import (
    can_manage_user,
    get_security_profile,
    record_audit,
    resolve_staff_role,
)


User = get_user_model()


def _validation_detail(error):
    return error.messages[0] if getattr(error, "messages", None) else str(error)


def _positive_int(value, default, maximum):
    try:
        return min(max(int(value), 1), maximum)
    except (TypeError, ValueError):
        return default


def _paginate(request, queryset_or_list, serializer):
    page_size = _positive_int(request.query_params.get("page_size"), 20, 100)
    paginator = Paginator(queryset_or_list, page_size)
    page_number = min(_positive_int(request.query_params.get("page"), 1, 1_000_000), max(paginator.num_pages, 1))
    page = paginator.get_page(page_number)
    return Response({
        "count": paginator.count,
        "page": page.number,
        "pages": paginator.num_pages,
        "page_size": page_size,
        "results": [serializer(item) for item in page.object_list],
    })


def _file_url(request, field):
    if not field:
        return ""
    url = field.url
    return request.build_absolute_uri(url) if url.startswith("/") else url


def _tag_definition_data(item):
    return {
        "id": item.id,
        "name": item.name,
        "color": item.color,
        "color_display": item.get_color_display(),
        "is_quick_preset": item.is_quick_preset,
        "sort_order": item.sort_order,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _tag_definition_values(data, *, partial=False):
    values = {}
    if not partial or "name" in data:
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("请输入标签名称。")
        if len(name) > 40:
            raise ValueError("标签名称不能超过 40 个字符。")
        values["name"] = name
    if not partial or "color" in data:
        color = str(data.get("color", TagDefinition.Color.SLATE)).strip()
        if color not in TagDefinition.Color.values:
            raise ValueError("请选择有效的标签颜色。")
        values["color"] = color
    if not partial or "is_quick_preset" in data:
        quick = data.get("is_quick_preset", False)
        if quick not in {True, False}:
            raise ValueError("快捷预设状态必须使用布尔值。")
        values["is_quick_preset"] = quick
    if not partial or "sort_order" in data:
        try:
            sort_order = int(data.get("sort_order", 0))
        except (TypeError, ValueError):
            raise ValueError("排序必须是整数。") from None
        if not 0 <= sort_order <= 65535:
            raise ValueError("排序必须位于 0 到 65535 之间。")
        values["sort_order"] = sort_order
    return values


def _user_data(user, actor=None):
    settings_obj = getattr(user, "journal_settings", None)
    role = resolve_staff_role(user) if user.is_staff else "user"
    security = get_security_profile(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "nickname": settings_obj.nickname if settings_obj else "",
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "staff_role": role,
        "staff_role_display": "超级管理员" if role == "superuser" else dict(StaffProfile.Role.choices).get(role, "未分配" if user.is_staff else "普通用户"),
        "email_verified": security.email_verified,
        "two_factor_enabled": security.two_factor_enabled,
        "last_login": user.last_login,
        "date_joined": user.date_joined,
        "entry_count": getattr(user, "entry_count", user.journal_entries.filter(deleted_at__isnull=True).count()),
        "column_count": getattr(user, "column_count", user.columns.filter(deleted_at__isnull=True).count()),
        "can_manage": can_manage_user(actor, user) if actor is not None else False,
    }


def _require_sensitive_reauthentication(request, target, action, *, force=False):
    if not force and not (target.is_staff or target.is_superuser):
        return
    actor_profile = get_security_profile(request.user)
    if not actor_profile.two_factor_enabled:
        record_audit(request, action="user.management_denied", target=target, metadata={"requested_action": action, "reason": "2fa_required"})
        raise DjangoValidationError("执行该管理员操作前必须启用两步验证。")
    try:
        authenticate_with_second_factor(
            request=request,
            username=request.user.get_username(),
            password=request.data.get("current_password", ""),
            otp=request.data.get("otp", ""),
            recovery_code=request.data.get("recovery_code", ""),
            staff_only=True,
        )
    except AuthenticationFailed as error:
        record_audit(request, action="user.management_denied", target=target, metadata={"requested_action": action, "reason": "reauthentication_failed"})
        raise DjangoValidationError("请重新验证当前密码和两步验证码。") from error


def _column_data(request, column, *, detail=False):
    data = {
        "id": column.id,
        "title": column.title,
        "summary": column.summary,
        "status": column.status,
        "status_display": column.get_status_display(),
        "featured": column.featured,
        "author": column.author.get_username(),
        "author_email": column.author.email,
        "entry_count": getattr(column, "entry_count", column.entries.filter(deleted_at__isnull=True).count()),
        "moderation_reason": column.moderation_reason,
        "moderated_at": column.moderated_at,
        "deleted_at": column.deleted_at,
        "deletion_reason": column.deletion_reason,
        "updated_at": column.updated_at,
        "published_at": column.published_at,
    }
    if detail:
        data.update({
            "body": column.body,
            "cover_url": _file_url(request, column.cover),
            "entries": [_entry_data(request, entry) for entry in column.entries.filter(deleted_at__isnull=True).select_related("user")],
        })
    return data


def _entry_data(request, entry, *, detail=False):
    data = {
        "id": entry.id,
        "title": entry.title,
        "japanese_title": entry.japanese_title,
        "user": entry.user.get_username(),
        "user_id": entry.user_id,
        "email": entry.user.email,
        "status": entry.get_watch_status_display(),
        "watch_status": entry.watch_status,
        "score": entry.personal_score,
        "visibility": entry.get_visibility_display(),
        "visibility_value": entry.visibility,
        "poster": _file_url(request, entry.poster_file) or entry.poster_url,
        "deleted_at": entry.deleted_at,
        "deletion_reason": entry.deletion_reason,
        "updated_at": entry.updated_at,
    }
    if detail:
        data.update({
            "airing_period": entry.airing_period,
            "studio": entry.studio,
            "episodes": entry.episodes,
            "description": entry.description,
            "tags": entry.tags,
            "tag_colors": entry.tag_colors,
            "review": entry.review,
        })
    return data


def _journal_data(request, settings_obj, *, detail=False):
    data = {
        "id": settings_obj.id,
        "user_id": settings_obj.user_id,
        "nickname": settings_obj.nickname or settings_obj.user.get_username(),
        "username": settings_obj.user.get_username(),
        "email": settings_obj.user.email,
        "public_status": settings_obj.public_status,
        "public_status_display": settings_obj.get_public_status_display(),
        "is_public": settings_obj.allow_sharing,
        "public_slug": settings_obj.public_slug,
        "entry_count": getattr(settings_obj, "entry_count", settings_obj.user.journal_entries.filter(deleted_at__isnull=True).count()),
        "review_reason": settings_obj.public_review_reason,
        "reviewed_at": settings_obj.public_reviewed_at,
        "updated_at": settings_obj.updated_at,
    }
    if detail:
        data.update({
            "showcase_subtitle": settings_obj.showcase_subtitle,
            "accent": settings_obj.accent,
            "avatar_url": _file_url(request, settings_obj.avatar),
            "entries": [_entry_data(request, item, detail=True) for item in settings_obj.user.journal_entries.filter(deleted_at__isnull=True)[:100]],
        })
    return data

