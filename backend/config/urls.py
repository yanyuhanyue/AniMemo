from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
import config.openapi  # noqa: F401 - registers the authentication extension
from journal.auth_views import CookieTokenRefreshView, EmailTokenObtainPairView
from plugin_host.views import PluginAssetView, PluginPreviewAssetView
from plugin_host.runtime.dispatch import PluginDispatch


def health(_request):
    return JsonResponse({"status": "ok", "service": "anime-journal-api"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/token/", EmailTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("api/", include("journal.urls")),
    path("api/integrations/v1/", include("integrations.urls")),
    path("plugin-assets/session/<str:asset_session>/<slug>/<version>/<path:asset>", PluginAssetView.as_view(), name="plugin-asset-session"),
    path("plugin-assets/<slug>/<version>/<path:asset>", PluginAssetView.as_view(), name="plugin-asset"),
    path("plugin-previews/session/<str:preview_session>/<slug>/<version>/<path:asset>", PluginPreviewAssetView.as_view(), name="plugin-preview-asset-session"),
    path("api/plugins/<slug>/<path:plugin_path>", PluginDispatch.as_view(), name="plugin-dispatch"),
    path("api/plugins/<slug>/", PluginDispatch.as_view(), name="plugin-dispatch-root"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
