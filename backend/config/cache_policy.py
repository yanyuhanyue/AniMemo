"""Response cache policy follows resolved API permissions and identity variants."""

from django.urls import Resolver404, resolve
from django.utils.cache import patch_vary_headers
from rest_framework.permissions import AllowAny

# These public authentication/callback operations still exchange private state.
PERSONAL_PUBLIC_MODULES = frozenset({
    "journal.auth_views",
    "journal.external_accounts.views",
})

# These views enforce private credentials or backend access inside dispatch.
PERSONAL_PUBLIC_VIEWS = frozenset({
    ("site_config.views", "InstallationSetupView"),
    ("plugin_host.runtime.dispatch", "PluginDispatch"),
})

# Their anonymous result is public; their authenticated result can be personal.
IDENTITY_VARIANTS = frozenset({
    ("journal.public_views", "PublicShowcaseView"),
    ("journal.public_catalog_views", "PublicCatalogEntriesView"),
    ("journal.public_catalog_views", "PublicCatalogSummaryView"),
    ("journal.public_catalog_views", "PublicCatalogFacetsView"),
    ("journal.public_catalog_views", "PublicCatalogDetailView"),
    ("journal.public_catalog_views", "PublicCatalogFieldView"),
    ("journal.public_catalog_views", "PublicCatalogDirectoryView"),
    ("journal.public_catalog_views", "RetiredPublicCatalogView"),
    ("plugin_host.views", "EnabledPluginListView"),
    ("plugin_host.views", "MarketplaceView"),
    ("plugin_host.views", "MarketplaceDetailView"),
})


class PrivateApiCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path_info.startswith("/api/"):
            return response
        match = getattr(request, "resolver_match", None)
        if match is None:
            # Earlier middleware may reject the request before normal routing.
            # Resolve the known path without invoking the view or parsing input.
            try:
                match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
            except Resolver404:
                return response
        view = getattr(getattr(match, "func", None), "cls", None)
        if view is None:
            return response
        view_identity = (view.__module__, view.__name__)
        identity_variant = view_identity in IDENTITY_VARIANTS
        requires_identity = any(permission is not AllowAny for permission in getattr(view, "permission_classes", ()))
        already_no_store = "no-store" in {
            item.strip().lower() for item in response.get("Cache-Control", "").split(",")
        }
        private = (
            requires_identity
            or view.__module__ in PERSONAL_PUBLIC_MODULES
            or view_identity in PERSONAL_PUBLIC_VIEWS
            or already_no_store
            or identity_variant and (
                getattr(getattr(request, "user", None), "is_authenticated", False)
                or bool(request.META.get("HTTP_AUTHORIZATION"))
            )
        )
        if private:
            response["Cache-Control"] = "private, no-store"
            response["Pragma"] = "no-cache"
        if private or identity_variant:
            patch_vary_headers(response, ("Authorization", "Cookie"))
        return response
