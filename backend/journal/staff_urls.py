from django.urls import path

from .staff_dashboard_views import (
    StaffColumnReviewView,
    StaffDashboardView,
    StaffPublicJournalReviewView,
    StaffUserPermissionsView,
)
from .public_views import StaffSiteSettingsView, StaffTestEmailView
from .staff_storage_views import (
    StaffMediaStorageActionView,
    StaffMediaStorageDetailView,
    StaffMediaStorageListView,
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
    path("system/media-storage/", StaffMediaStorageListView.as_view(), name="staff-media-storage-list"),
    path("system/media-storage/<int:pk>/", StaffMediaStorageDetailView.as_view(), name="staff-media-storage-detail"),
    path("system/media-storage/<int:pk>/actions/", StaffMediaStorageActionView.as_view(), name="staff-media-storage-action"),
    path("security/two-factor/", StaffTwoFactorView.as_view(), name="staff-two-factor"),
]
