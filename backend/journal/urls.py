from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .auth_views import (
    AccountView,
    CompleteRegistrationView,
    CsrfTokenView,
    LogoutView,
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetView,
    RegisterView,
    StaffLoginView,
    VerifyRegistrationView,
)
from .entry_views import (
    ColumnViewSet,
    JournalEntryViewSet,
    MeView,
    PublicJournalStatusView,
    QuickFilterViewSet,
    UserSettingsView,
)
from .import_export_views import ExportEntriesView, ImportEntriesView
from .public_views import (
    FeaturedColumnsView,
    PublicCatalogSearchView,
    PublicHomepageView,
    PublicShowcaseListView,
    PublicShowcaseView,
    PublicSiteSettingsView,
    SharedEntryView,
    TagPresetListView,
)


router = DefaultRouter()
router.register("entries", JournalEntryViewSet, basename="entry")
router.register("filters", QuickFilterViewSet, basename="filter")
router.register("columns", ColumnViewSet, basename="column")

urlpatterns = [
    path("", include(router.urls)),
    path("", include("journal.watch_history.urls")),
    path("", include("journal.external_media.urls")),
    path("", include("journal.analytics.urls")),
    path("", include("journal.external_sync.urls")),
    path("", include("plugin_host.urls")),
    path("external-accounts/", include("journal.external_accounts.urls")),
    path("staff/", include("journal.staff_urls")),
    path("site-settings/", PublicSiteSettingsView.as_view(), name="site-settings"),
    path("tag-presets/", TagPresetListView.as_view(), name="tag-presets"),
    path("homepage/", PublicHomepageView.as_view(), name="homepage"),
    path("auth/register/request/", RegisterView.as_view(), name="register-request"),
    path("auth/register/verify/", VerifyRegistrationView.as_view(), name="register-verify"),
    path("auth/register/complete/", CompleteRegistrationView.as_view(), name="register-complete"),
    path("auth/staff-login/", StaffLoginView.as_view(), name="staff-login"),
    path("auth/csrf/", CsrfTokenView.as_view(), name="csrf-token"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/password-reset/", PasswordResetView.as_view(), name="password-reset"),
    path("auth/password-reset-confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("auth/password-change/", PasswordChangeView.as_view(), name="password-change"),
    path("auth/account/", AccountView.as_view(), name="account"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("settings/me/", UserSettingsView.as_view(), name="settings"),
    path("public-journal/status/", PublicJournalStatusView.as_view(), name="public-journal-status"),
    path("import/", ImportEntriesView.as_view(), name="import"),
    path("export/", ExportEntriesView.as_view(), name="export"),
    path("showcase/<uuid:public_slug>/", PublicShowcaseView.as_view(), name="showcase"),
    path("showcases/", PublicShowcaseListView.as_view(), name="showcase-list"),
    path("shared/<uuid:share_slug>/", SharedEntryView.as_view(), name="shared-entry"),
    path("featured/", FeaturedColumnsView.as_view(), name="featured"),
    path("catalog/public-search/", PublicCatalogSearchView.as_view(), name="public-catalog-search"),
]
