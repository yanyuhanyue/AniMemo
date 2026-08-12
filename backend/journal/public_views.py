from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from site_config.models import InstallationState, SiteSettings, TagDefinition

from .emails import EmailDeliveryDisabled, EmailDeliveryError, EmailDeliveryNotConfigured, send_transactional_email
from .models import Column, JournalEntry, UserSettings
from .serializers import ColumnSerializer, JournalEntrySerializer, SiteSettingsSerializer, StaffSiteSettingsSerializer, TestEmailSerializer
from .staff_services import StaffCapabilityPermission, record_audit
from .view_helpers import build_public_stats


User = get_user_model()


class PublicSiteSettingsView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        serializer = SiteSettingsSerializer(SiteSettings.load(), context={"request": request})
        data = dict(serializer.data)
        data["registration_enabled"] = bool(
            data.get("registration_enabled") and InstallationState.is_initialized()
        )
        return Response(data)


class TagPresetListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        presets = TagDefinition.objects.filter(is_quick_preset=True)
        return Response({
            "results": [
                {
                    "id": item.id,
                    "name": item.name,
                    "color": item.color,
                    "sort_order": item.sort_order,
                }
                for item in presets
            ],
        })


class StaffSiteSettingsView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        serializer = StaffSiteSettingsSerializer(SiteSettings.load(), context={"request": request})
        return Response(serializer.data)

    def patch(self, request):
        settings_obj = SiteSettings.load()
        before = StaffSiteSettingsSerializer(settings_obj, context={"request": request}).data
        serializer = StaffSiteSettingsSerializer(
            settings_obj,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record_audit(request, action="settings.update", target=settings_obj, before=before, after=serializer.data)
        return Response(serializer.data)


class StaffTestEmailView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request):
        serializer = TestEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recipient = serializer.validated_data["email"]
        site_settings = SiteSettings.load()
        try:
            result = send_transactional_email(
                to=recipient,
                subject=f"{site_settings.site_name} 邮件服务测试",
                html=(
                    f"<h1>{site_settings.site_name}</h1>"
                    "<p>这是一封管理员后台发出的测试邮件。</p>"
                    "<p>收到此邮件表示激活邮件服务配置正确。</p>"
                ),
                text=f"{site_settings.site_name} 邮件服务测试成功。",
            )
        except EmailDeliveryDisabled as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except EmailDeliveryNotConfigured as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        provider_id = result.get("id", "") if isinstance(result, dict) else ""
        detail = (
            "开发环境未配置 Resend，邮件内容已写入后端日志，未实际发送。"
            if provider_id == "development-console"
            else f"测试邮件已发送至 {recipient}。"
        )
        record_audit(request, action="settings.test_email", target=site_settings, metadata={"recipient": recipient, "provider_id": provider_id})
        return Response({"detail": detail, "provider_id": provider_id})


class PublicShowcaseView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, public_slug):
        owner_settings = get_object_or_404(UserSettings.objects.select_related("user"), public_slug=public_slug)
        is_owner_preview = request.user.is_authenticated and request.user.pk == owner_settings.user_id
        if not is_owner_preview and (
            owner_settings.public_status != UserSettings.PublicStatus.APPROVED
            or not owner_settings.allow_sharing
        ):
            return Response({"detail": "该手账未公开。"}, status=status.HTTP_404_NOT_FOUND)
        showcase_entries = JournalEntry.objects.filter(
            user=owner_settings.user,
            deleted_at__isnull=True,
        ).prefetch_related("external_identities")
        if not is_owner_preview:
            showcase_entries = showcase_entries.filter(visibility=JournalEntry.Visibility.PUBLIC)
        summary_records = list(showcase_entries.values(
            "title", "airing_period", "tags", "personal_score", "watch_status",
        ))
        stats = build_public_stats(summary_records)

        entries = showcase_entries
        query = request.query_params.get("search", "").strip()
        tag = request.query_params.get("tag", "").strip()
        watch_status = request.query_params.get("status", "").strip()
        if query:
            entries = entries.filter(Q(title__icontains=query) | Q(japanese_title__icontains=query))
        if watch_status:
            entries = entries.filter(watch_status=watch_status)
        results = JournalEntrySerializer(entries, many=True, context={"request": request}).data
        if tag:
            results = [entry for entry in results if tag in entry["tags"]]
        avatar_url = ""
        if owner_settings.avatar:
            avatar_url = owner_settings.avatar.url
            if avatar_url.startswith("/"):
                avatar_url = request.build_absolute_uri(avatar_url)
        return Response({
            "profile": {
                "nickname": owner_settings.nickname or owner_settings.user.username,
                "subtitle": owner_settings.showcase_subtitle,
                "avatar_url": avatar_url,
                "accent": owner_settings.accent,
                "public_slug": owner_settings.public_slug,
            },
            "stats": stats,
            "results": results,
        })


class PublicShowcaseListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        settings_items = UserSettings.objects.select_related("user").filter(
            public_status=UserSettings.PublicStatus.APPROVED,
            allow_sharing=True,
            user__journal_entries__visibility=JournalEntry.Visibility.PUBLIC,
            user__journal_entries__deleted_at__isnull=True,
        ).distinct().order_by("-updated_at")
        query = request.query_params.get("search", "").strip()
        if query:
            settings_items = settings_items.filter(
                Q(nickname__icontains=query)
                | Q(user__username__icontains=query)
                | Q(showcase_subtitle__icontains=query)
            )

        results = []
        for settings_obj in settings_items[:60]:
            public_entries = JournalEntry.objects.filter(
                user=settings_obj.user,
                visibility=JournalEntry.Visibility.PUBLIC,
                deleted_at__isnull=True,
            ).prefetch_related("external_identities")
            summary_records = list(public_entries.values(
                "title", "airing_period", "tags", "personal_score", "watch_status",
            ))
            top_picks = public_entries.exclude(personal_score__isnull=True).order_by("-personal_score", "-updated_at")[:3]
            avatar_url = ""
            if settings_obj.avatar:
                avatar_url = settings_obj.avatar.url
                if avatar_url.startswith("/"):
                    avatar_url = request.build_absolute_uri(avatar_url)
            results.append({
                "nickname": settings_obj.nickname or settings_obj.user.username,
                "username": settings_obj.user.username,
                "subtitle": settings_obj.showcase_subtitle,
                "avatar_url": avatar_url,
                "public_slug": settings_obj.public_slug,
                "stats": build_public_stats(summary_records),
                "top_picks": JournalEntrySerializer(top_picks, many=True, context={"request": request}).data,
            })
        return Response({"count": len(results), "results": results})


class SharedEntryView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, share_slug):
        entry = get_object_or_404(
            JournalEntry.objects.prefetch_related("external_identities"),
            share_slug=share_slug,
            deleted_at__isnull=True,
        )
        settings_obj, _ = UserSettings.objects.get_or_create(user=entry.user, defaults={"nickname": entry.user.username})
        if (
            entry.visibility == JournalEntry.Visibility.PRIVATE
            or settings_obj.public_status != UserSettings.PublicStatus.APPROVED
            or not settings_obj.allow_sharing
        ):
            return Response({"detail": "该记录未公开。"}, status=status.HTTP_404_NOT_FOUND)
        return Response(JournalEntrySerializer(entry, context={"request": request}).data)


class FeaturedColumnsView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        columns = Column.objects.filter(status=Column.Status.APPROVED, deleted_at__isnull=True).select_related("author").prefetch_related("entries")
        if request.query_params.get("featured") == "true":
            columns = columns.filter(featured=True)
        return Response(ColumnSerializer(columns[:60], many=True, context={"request": request}).data)


class PublicCatalogSearchView(APIView):
    """Search the catalogue records contributed by administrator accounts."""

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        try:
            page_size = min(max(int(request.query_params.get("page_size", 10)), 1), 30)
        except (TypeError, ValueError):
            page_size = 10
        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        entries = JournalEntry.objects.filter(
            user__is_staff=True,
            deleted_at__isnull=True,
        ).prefetch_related("external_identities")
        if query:
            entries = entries.filter(
                Q(title__icontains=query)
                | Q(japanese_title__icontains=query)
                | Q(studio__icontains=query)
                | Q(airing_period__icontains=query)
            )
        entries = entries.order_by("-updated_at")
        count = entries.count()
        pages = max(1, (count + page_size - 1) // page_size)
        page = min(page, pages)
        entries = entries[(page - 1) * page_size: page * page_size]
        serialized = JournalEntrySerializer(entries, many=True, context={"request": request}).data
        public_fields = [
            "id", "title", "japanese_title", "airing_period", "studio", "episodes",
            "description", "poster_url", "poster", "baike_url", "tags",
        ]
        results = [{key: item.get(key) for key in public_fields} for item in serialized]
        return Response({
            "count": count,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "results": results,
        })


class PublicHomepageView(APIView):
    """Return the live homepage catalogue for the configured administrator account."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        site_settings = SiteSettings.load()
        owner = site_settings.homepage_owner
        if not owner or not owner.is_staff or not owner.is_active:
            owner = User.objects.filter(is_staff=True, is_active=True).order_by("id").first()
        entries = JournalEntry.objects.filter(
            user=owner,
            deleted_at__isnull=True,
        ).prefetch_related("external_identities").order_by("-updated_at", "-id") if owner else JournalEntry.objects.none()
        summary_records = list(entries.values(
            "title", "airing_period", "tags", "personal_score", "watch_status",
        ))
        return Response({
            "stats": build_public_stats(summary_records),
            "results": JournalEntrySerializer(entries, many=True, context={"request": request}).data,
        })

