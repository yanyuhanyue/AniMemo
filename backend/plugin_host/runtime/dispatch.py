from django.db import transaction
from django.http import Http404
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from plugin_host.models import PluginInstallation
from plugin_host.permissions import can_access_plugin_backend

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
                permission_code=resolved.permission,
            ):
                return Response({"detail": "没有权限调用此插件接口。"}, status=status.HTTP_403_FORBIDDEN)
            result = resolved.handler(request, **resolved.kwargs)
        except Http404:
            raise
        except RuntimeUnavailable:
            raise Http404
        except RuntimeLoadError as error:
            with transaction.atomic():
                locked = PluginInstallation.objects.select_for_update().filter(slug=slug).first()
                if locked is not None:
                    locked.healthy = False
                    locked.status = PluginInstallation.Status.UNHEALTHY
                    locked.last_error = str(error)
                    locked.save(update_fields=["healthy", "status", "last_error", "updated_at"])
            return Response({"detail": "插件 Runtime 无法加载。"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception:
            return Response({"detail": "插件处理请求时发生错误。"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
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
