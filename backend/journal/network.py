import ipaddress

from django.conf import settings


def _is_trusted_proxy(value):
    try:
        remote = ipaddress.ip_address(value)
    except ValueError:
        return False
    for configured in getattr(settings, "TRUSTED_PROXY_IPS", []):
        try:
            if remote in ipaddress.ip_network(configured, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request):
    remote = str(request.META.get("REMOTE_ADDR") or "").strip()
    if remote and _is_trusted_proxy(remote):
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "")
        raw_candidates = [item.strip() for item in forwarded.split(",") if item.strip()]
        candidates = []
        for item in raw_candidates:
            try:
                candidates.append(str(ipaddress.ip_address(item)))
            except ValueError:
                return remote
        chain = [*candidates, str(ipaddress.ip_address(remote))]
        for candidate in reversed(chain):
            if not _is_trusted_proxy(candidate):
                return candidate
        if candidates:
            return candidates[0]
    try:
        return str(ipaddress.ip_address(remote)) if remote else None
    except ValueError:
        return None
