from django.contrib import admin

from accounts.models import LoginEvent, PendingRegistration, StaffProfile, UserSecurityProfile
from site_config.models import CloudflareR2Account, MediaObject, MediaStorageBackend, MediaStoragePoolSettings, SiteSettings, TagDefinition
from .models import (
    AdminAuditLog,
    Column,
    ExternalMediaIdentity,
    JournalEntry,
    QuickFilter,
    UserExternalAccountConnection,
    UserSettings,
)


@admin.register(PendingRegistration)
class PendingRegistrationAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at", "expires_at", "verified_at", "consumed_at", "resend_count")
    list_filter = ("verified_at", "consumed_at")
    search_fields = ("email",)
    readonly_fields = (
        "email", "token_hash", "created_at", "expires_at", "verified_at", "consumed_at",
        "completion_token_hash", "completion_token_expires_at", "requested_ip", "user_agent_digest",
        "resend_count", "last_sent_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("基础品牌", {"fields": ("site_name", "site_avatar", "social_handle")}),
        ("公共页面文案", {"fields": ("homepage_owner", "homepage_title", "homepage_description", "universe_description")}),
        ("账号策略", {"fields": ("registration_enabled",)}),
        ("邮件服务", {"fields": ("email_delivery_enabled", "email_sender_name", "email_sender_address")}),
        ("系统信息", {"fields": ("updated_at",)}),
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MediaStorageBackend)
class MediaStorageBackendAdmin(admin.ModelAdmin):
    list_display = ("name", "backend_type", "priority", "enabled", "accept_new_writes", "config_version", "updated_at")
    list_filter = ("backend_type", "enabled", "accept_new_writes")
    readonly_fields = (
        "encrypted_access_key_id", "encrypted_secret_access_key",
        "usage_payload_bytes", "usage_metadata_bytes", "usage_object_count", "usage_refreshed_at",
        "config_version", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(MediaStoragePoolSettings)
class MediaStoragePoolSettingsAdmin(admin.ModelAdmin):
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(CloudflareR2Account)
class CloudflareR2AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "account_id", "warning_bytes", "write_limit_bytes", "updated_at")
    readonly_fields = (
        "id", "account_id", "name", "warning_bytes", "write_limit_bytes",
        "encrypted_analytics_token", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(MediaObject)
class MediaObjectAdmin(admin.ModelAdmin):
    list_display = ("id", "storage_backend", "object_key", "size_bytes", "content_type", "created_at")
    search_fields = ("id", "object_key", "sha256")
    readonly_fields = ("id", "storage_backend", "object_key", "size_bytes", "content_type", "sha256", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "watch_status", "personal_score", "visibility", "updated_at")
    list_filter = ("watch_status", "visibility")
    search_fields = ("title", "japanese_title", "user__username", "user__email")
    readonly_fields = ("share_slug", "created_at", "updated_at")


@admin.register(ExternalMediaIdentity)
class ExternalMediaIdentityAdmin(admin.ModelAdmin):
    list_display = ("provider", "external_id", "entry", "entry_user", "metadata_fetched_at", "updated_at")
    list_filter = ("provider",)
    search_fields = ("external_id", "entry__title", "entry__user__username", "entry__user__email")
    readonly_fields = (
        "entry", "provider", "external_id", "canonical_url", "metadata", "metadata_fetched_at",
        "provider_updated_at", "created_at", "updated_at",
    )

    @admin.display(description="用户")
    def entry_user(self, obj):
        return obj.entry.user

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UserExternalAccountConnection)
class UserExternalAccountConnectionAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "external_username", "status", "auth_method", "verified_at")
    list_filter = ("provider", "status", "auth_method")
    search_fields = ("user__username", "user__email", "external_username", "display_name")
    exclude = ("credential_ciphertext",)
    readonly_fields = (
        "user", "provider", "auth_method", "external_user_id", "external_username", "display_name",
        "credential_key_version", "metadata", "status", "connected_at", "verified_at", "last_used_at",
        "expires_at", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserSettings)
class UserSettingsAdmin(admin.ModelAdmin):
    list_display = ("nickname", "user", "public_status", "allow_sharing", "updated_at")
    list_filter = ("public_status", "allow_sharing")
    search_fields = ("nickname", "user__username", "user__email")
    readonly_fields = ("public_slug", "updated_at")

    def save_model(self, request, obj, form, change):
        obj.allow_sharing = obj.public_status == UserSettings.PublicStatus.APPROVED
        super().save_model(request, obj, form, change)


@admin.register(QuickFilter)
class QuickFilterAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "match_mode", "color")


@admin.register(TagDefinition)
class TagDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "color", "is_quick_preset", "sort_order", "updated_at")
    list_filter = ("is_quick_preset", "color")
    search_fields = ("name",)
    ordering = ("sort_order", "id")


@admin.register(Column)
class ColumnAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "status", "featured", "published_at", "updated_at")
    list_filter = ("status", "featured")
    search_fields = ("title", "summary", "author__username")
    readonly_fields = ("slug", "created_at", "updated_at")
    filter_horizontal = ("entries",)


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "updated_by", "updated_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email")


@admin.register(UserSecurityProfile)
class UserSecurityProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "email_verified", "two_factor_enabled", "session_version", "updated_at")
    list_filter = ("email_verified", "two_factor_enabled")
    search_fields = ("user__username", "user__email")
    readonly_fields = (
        "totp_secret_encrypted",
        "pending_totp_secret_encrypted",
        "pending_totp_created_at",
        "recovery_code_hashes",
        "updated_at",
    )


@admin.register(LoginEvent)
class LoginEventAdmin(admin.ModelAdmin):
    list_display = ("user", "account", "event_type", "success", "ip_address", "created_at")
    list_filter = ("event_type", "success")
    search_fields = ("account", "user__username", "user__email", "ip_address")
    readonly_fields = ("user", "account", "event_type", "success", "ip_address", "user_agent", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("actor", "action", "target_type", "target_label", "ip_address", "created_at")
    list_filter = ("action", "target_type")
    search_fields = ("actor__username", "action", "target_label", "target_id", "ip_address")
    readonly_fields = ("actor", "action", "target_type", "target_id", "target_label", "before", "after", "metadata", "ip_address", "user_agent", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
