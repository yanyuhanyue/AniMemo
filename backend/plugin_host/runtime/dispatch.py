from config.api_errors import public_failure
from django.db import transaction
from django.http import Http404
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from plugin_host.models import PluginDeployment
from plugin_host.permissions import can_access_plugin_backend
from plugin_host.public_diagnostics import PLUGIN_RUNTIME_UNAVAILABLE

from .registry import RuntimeLoadError, RuntimeUnavailable, runtime_registry


class PluginDispatch(APIView):
    permission_classes = [permissions.AllowAny]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def _dispatch(self, request, slug, plugin_path=""):
        try:
            candidate = runtime_registry.ensure_current(slug)
            if candidate.plugin is None:
                raise Http404
            resolved = candidate.context.api.resolve(request.method, str(plugin_path or "").strip("/"))
            if resolved is None:
                raise Http404
            if not can_access_plugin_backend(
                request.user,
                slug,
                candidate.manifest,
                access=resolved.access,
                permission_code=resolved.permission,
            ):
                return Response({"detail": "没有权限调用此插件接口。"}, status=status.HTTP_403_FORBIDDEN)
            result = resolved.handler(request, **resolved.kwargs)
        except Http404:
            raise
        except RuntimeUnavailable:
            raise Http404
        except RuntimeLoadError:
            with transaction.atomic():
                locked = PluginDeployment.objects.select_for_update().filter(plugin__slug=slug).first()
                if locked is not None:
                    locked.healthy = False
                    locked.status = PluginDeployment.Status.UNHEALTHY
                    locked.last_error = PLUGIN_RUNTIME_UNAVAILABLE
                    locked.save(update_fields=["healthy", "status", "last_error", "updated_at"])
            return Response(
                public_failure(
                    request=request,
                    candidate_code=PLUGIN_RUNTIME_UNAVAILABLE,
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                ),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            return Response(
                public_failure(
                    request=request,
                    candidate_code="internal_error",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                ),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if isinstance(result, Response):
            return result
        if isinstance(result, tuple) and len(result) == 2:
            return Response(result[0], status=result[1])
        return Response(result)

    get = _dispatch
    post = _dispatch
    put = _dispatch
    patch = _dispatch
    delete = _dispatch
