from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.defaults import (
    bad_request as default_bad_request,
)
from django.views.defaults import (
    page_not_found as default_page_not_found,
)
from django.views.defaults import (
    permission_denied as default_permission_denied,
)
from django.views.defaults import (
    server_error as default_server_error,
)
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView
from plugin_host.runtime.dispatch import PluginDispatch
from plugin_host.views import PluginAssetView, PluginPreviewAssetView

import config.openapi  # noqa: F401 - registers the authentication extension

from .api_errors import public_failure
from .api_urls import urlpatterns as core_api_urlpatterns


def _is_api_request(request):
    path_info = getattr(request, "path_info", "")
    return path_info == "/api" or path_info.startswith("/api/")


def _api_failure_response(request, *, candidate_code, status_code):
    failure = public_failure(
        request=request,
        candidate_code=candidate_code,
        status_code=status_code,
    )
    response = JsonResponse(failure, status=status_code)
    response["X-AniMemo-Correlation-ID"] = failure["correlation_id"]
    return response


def api_bad_request(request, exception=None):
    if _is_api_request(request):
        return _api_failure_response(
            request,
            candidate_code="invalid_request",
            status_code=400,
        )
    return default_bad_request(request, exception)


def api_permission_denied(request, exception=None):
    if _is_api_request(request):
        return _api_failure_response(
            request,
            candidate_code="permission_denied",
            status_code=403,
        )
    return default_permission_denied(request, exception)


def api_page_not_found(request, exception=None):
    if _is_api_request(request):
        return _api_failure_response(
            request,
            candidate_code="not_found",
            status_code=404,
        )
    return default_page_not_found(request, exception)


def api_server_error(request):
    if _is_api_request(request):
        return _api_failure_response(
            request,
            candidate_code="internal_error",
            status_code=500,
        )
    return default_server_error(request)


handler400 = api_bad_request
handler403 = api_permission_denied
handler404 = api_page_not_found
handler500 = api_server_error


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

if settings.ANIMEMO_ISOLATED_CAPACITY_PROBE:
    urlpatterns += [
        path(
            "api/v1/_isolated/capacity/",
            include("performance.urls"),
        )
    ]
