from django.conf import settings


class ContentSecurityPolicyMiddleware:
    """Emit the deployment-controlled CSP without widening plugin execution."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "SECURE_CONTENT_SECURITY_POLICY", "")
        if policy:
            header = "Content-Security-Policy-Report-Only" if settings.CSP_REPORT_ONLY else "Content-Security-Policy"
            response[header] = policy
        return response
