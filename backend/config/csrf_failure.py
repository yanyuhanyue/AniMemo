from django.http import JsonResponse
from django.views.csrf import csrf_failure as default_csrf_failure

from .api_errors import public_failure


def csrf_failure(request, reason=""):
    """Return the stable JSON contract for API CSRF failures."""
    if request.path.startswith("/api/"):
        failure = public_failure(
            request=request,
            candidate_code="csrf_failed",
            status_code=403,
        )
        response = JsonResponse(failure, status=403)
        response["X-AniMemo-Correlation-ID"] = failure["correlation_id"]
        return response
    return default_csrf_failure(request, reason)
