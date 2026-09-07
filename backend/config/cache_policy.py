"""Response cache policy follows resolved API permissions and identity variants."""

from django.utils.cache import patch_vary_headers
from rest_framework.permissions import AllowAny

# These public authentication/callback operations still exchange private state.
PERSONAL_PUBLIC_MODULES = frozenset({
    "journal.auth_views",
    "journal.external_accounts.views",
})

# Their anonymous result is public; their authenticated result can be personal.
IDENTITY_VARIANTS = frozenset({
    ("journal.public_views", "PublicShowcaseView"),
    ("plugin_host.views", "EnabledPluginListView"),
    ("plugin_host.views", "MarketplaceView"),
    ("plugin_host.views", "MarketplaceDetailView"),
    ("plugin_host.runtime.dispatch", "PluginDispatch"),
})


class PrivateApiCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path_info.startswith("/api/"):
            return response
        match = getattr(request, "resolver_match", None)
        view = getattr(getattr(match, "func", None), "cls", None)
        if view is None:
            return response
        identity_variant = (view.__module__, view.__name__) in IDENTITY_VARIANTS
        requires_identity = any(permission is not AllowAny for permission in getattr(view, "permission_classes", ()))
        already_no_store = "no-store" in {
            item.strip().lower() for item in response.get("Cache-Control", "").split(",")
        }
        private = (
            requires_identity
            or view.__module__ in PERSONAL_PUBLIC_MODULES
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
