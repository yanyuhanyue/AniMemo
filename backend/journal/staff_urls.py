from django.urls import path

from .staff_dashboard_views import (
    StaffColumnReviewView,
    StaffDashboardView,
    StaffPublicJournalReviewView,
    StaffUserPermissionsView,
)
from .public_views import StaffSiteSettingsView, StaffTestEmailView
from .external_accounts.staff_views import (
    StaffExternalProviderClientSecretView,
    StaffExternalProviderConfigurationView,
)
from .staff_storage_views import (
    StaffMediaStorageActionView,
    StaffMediaStorageDetailView,
    StaffMediaStorageListView,
)
from .staff_update_views import (
    StaffUpdateApplyView,
    StaffUpdateLogsView,
    StaffUpdateOperationView,
    StaffUpdatePlanView,
    StaffUpdateReleasesView,
    StaffUpdateRollbackView,
    StaffUpdateStatusView,
)
from .staff_views import (
    StaffBackupView,
    StaffBulkActionView,
    StaffResourceDetailView,
    StaffResourceListView,
    StaffSystemHealthView,
    StaffTagDefinitionDetailView,
    StaffTagDefinitionListCreateView,
    StaffTwoFactorView,
    StaffUserActionView,
    StaffUserDetailView,
)


urlpatterns = [
    path("dashboard/", StaffDashboardView.as_view(), name="staff-dashboard"),
    path("site-settings/", StaffSiteSettingsView.as_view(), name="staff-site-settings"),
    path("site-settings/test-email/", StaffTestEmailView.as_view(), name="staff-test-email"),
    path(
        "external-providers/<slug:provider>/",
        StaffExternalProviderConfigurationView.as_view(),
        name="staff-external-provider-configuration",
    ),
    path(
        "external-providers/<slug:provider>/client-secret/",
        StaffExternalProviderClientSecretView.as_view(),
        name="staff-external-provider-client-secret",
    ),
    path("columns/<int:pk>/review/", StaffColumnReviewView.as_view(), name="staff-column-review"),
    path("public-journals/<int:pk>/review/", StaffPublicJournalReviewView.as_view(), name="staff-public-journal-review"),
    path("users/<int:pk>/permissions/", StaffUserPermissionsView.as_view(), name="staff-user-permissions"),
    path("resources/<str:kind>/", StaffResourceListView.as_view(), name="staff-resource-list"),
    path("resources/<str:kind>/<int:pk>/", StaffResourceDetailView.as_view(), name="staff-resource-detail"),
    path("bulk/<str:kind>/", StaffBulkActionView.as_view(), name="staff-bulk-action"),
    path("users/<int:pk>/detail/", StaffUserDetailView.as_view(), name="staff-user-detail"),
    path("users/<int:pk>/<str:action>/", StaffUserActionView.as_view(), name="staff-user-action"),
    path("tags/", StaffTagDefinitionListCreateView.as_view(), name="staff-tag-list"),
    path("tags/<int:pk>/", StaffTagDefinitionDetailView.as_view(), name="staff-tag-detail"),
    path("system/health/", StaffSystemHealthView.as_view(), name="staff-system-health"),
    path("system/backup/", StaffBackupView.as_view(), name="staff-system-backup"),
    path("system/updates/status/", StaffUpdateStatusView.as_view(), name="staff-update-status"),
    path("system/updates/releases/", StaffUpdateReleasesView.as_view(), name="staff-update-releases"),
    path("system/updates/plan/", StaffUpdatePlanView.as_view(), name="staff-update-plan"),
    path("system/updates/apply/", StaffUpdateApplyView.as_view(), name="staff-update-apply"),
    path("system/updates/rollback/", StaffUpdateRollbackView.as_view(), name="staff-update-rollback"),
    path("system/updates/operations/<str:operation_id>/", StaffUpdateOperationView.as_view(), name="staff-update-operation"),
    path("system/updates/operations/<str:operation_id>/logs/", StaffUpdateLogsView.as_view(), name="staff-update-logs"),
    path("system/media-storage/", StaffMediaStorageListView.as_view(), name="staff-media-storage-list"),
    path("system/media-storage/<int:pk>/", StaffMediaStorageDetailView.as_view(), name="staff-media-storage-detail"),
    path("system/media-storage/<int:pk>/actions/", StaffMediaStorageActionView.as_view(), name="staff-media-storage-action"),
    path("security/two-factor/", StaffTwoFactorView.as_view(), name="staff-two-factor"),
]
