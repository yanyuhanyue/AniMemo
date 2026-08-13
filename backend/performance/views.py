import time

from django.conf import settings
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView


class IsolatedProviderLatencyView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, _request):
        time.sleep(settings.ANIMEMO_ISOLATED_PROVIDER_LATENCY_MS / 1000)
        return Response(
            {
                "provider": "fake-bangumi-provider",
                "network": "disabled",
                "latency_ms": settings.ANIMEMO_ISOLATED_PROVIDER_LATENCY_MS,
            }
        )
