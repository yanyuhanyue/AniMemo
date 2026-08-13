from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView
import config.openapi  # noqa: F401 - registers the authentication extension
from plugin_host.views import PluginAssetView, PluginPreviewAssetView
from plugin_host.runtime.dispatch import PluginDispatch

from .api_urls import urlpatterns as core_api_urlpatterns


class CSPCompatibleSwaggerView(SpectacularSwaggerSplitView):
    """Serve sidecar assets and the initializer as same-origin resources."""

    template_name = "drf_spectacular/swagger_ui_split.html"


def health(_request):
    return JsonResponse(
        {
            "status": "ok",
            "service": "animemo-api",
            "release": {
                "version": settings.ANIMEMO_VERSION,
                "commit": settings.ANIMEMO_COMMIT,
                "channel": settings.ANIMEMO_RELEASE_CHANNEL,
            },
            "artifact": {
                "version": settings.ANIMEMO_ARTIFACT_VERSION,
                "commit": settings.ANIMEMO_ARTIFACT_COMMIT,
                "channel": settings.ANIMEMO_ARTIFACT_CHANNEL,
            },
            "contracts": {
                "database": settings.ANIMEMO_DATABASE_CONTRACT,
                "configuration": settings.ANIMEMO_CONFIGURATION_CONTRACT,
            },
        }
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", CSPCompatibleSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/v1/", include((core_api_urlpatterns, "api-v1"), namespace="api-v1")),
    path("api/", include(core_api_urlpatterns)),
    path("api/integrations/v1/", include("integrations.urls")),
    path("plugin-assets/session/<str:asset_session>/<slug>/<version>/<path:asset>", PluginAssetView.as_view(), name="plugin-asset-session"),
    path("plugin-assets/<slug>/<version>/<path:asset>", PluginAssetView.as_view(), name="plugin-asset"),
    path("plugin-previews/session/<str:preview_session>/<slug>/<version>/<path:asset>", PluginPreviewAssetView.as_view(), name="plugin-preview-asset-session"),
    path("api/v1/plugins/<slug>/<path:plugin_path>", PluginDispatch.as_view(), name="plugin-dispatch-v1"),
    path("api/v1/plugins/<slug>/", PluginDispatch.as_view(), name="plugin-dispatch-root-v1"),
    path("api/plugins/<slug>/<path:plugin_path>", PluginDispatch.as_view(), name="plugin-dispatch"),
    path("api/plugins/<slug>/", PluginDispatch.as_view(), name="plugin-dispatch-root"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
