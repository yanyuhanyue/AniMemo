from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import AnalyticsRangeError, build_user_analytics


class MyStatsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            result = build_user_analytics(
                user=request.user,
                start=request.query_params.get("start"),
                end=request.query_params.get("end"),
            )
        except AnalyticsRangeError as error:
            return Response(
                {"code": "invalid_analytics_range", "detail": str(error)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(result)
