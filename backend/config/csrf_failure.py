from django.http import JsonResponse
from django.views.csrf import csrf_failure as default_csrf_failure


def csrf_failure(request, reason=""):
    """Return the stable JSON contract for API CSRF failures."""
    if request.path.startswith("/api/"):
        return JsonResponse(
            {"code": "csrf_failed", "detail": "安全验证已过期，请刷新页面后重试。"},
            status=403,
        )
    return default_csrf_failure(request, reason)
